"""Embedding service — converts text into dense vectors for semantic search.

This module is the bridge between human-readable code and the vector space
where similarity search happens. It calls Ollama's local embedding endpoint.

HOW EMBEDDINGS WORK (for newbies):
- An embedding model (mxbai-embed-large) takes text and outputs a 1024-dimensional
  float vector. Semantically similar texts get vectors that point in similar directions.
- "JWT authentication" and "login token validation" would have high cosine similarity
  (~0.85) even though they share no words, because the model learned they mean
  similar things during pre-training.
- "JWT authentication" and "chocolate cake recipe" would have low cosine similarity
  (~0.15) — different semantic meaning.

THREADING MODEL:
- embed_text() is async — it uses httpx.AsyncClient which runs on the asyncio
  event loop. No threads are spawned. The HTTP call to Ollama is non-blocking.
- embed_texts() uses asyncio.gather() for batch parallelism — multiple embedding
  requests to Ollama run concurrently on the same event loop.
- Rate limiting (0.1s sleep between batches) prevents overwhelming Ollama
  when indexing hundreds of chunks.

PITFALL #22: Ollama silently truncates text longer than 512 tokens.
- mxbai-embed-large has a 512-token context window
- 1 token ≈ 3 chars for code (more whitespace/symbols than English)
- We pre-truncate to 1500 chars to stay safe
- WITHOUT truncation: Ollama returns embeddings for the FIRST 512 tokens only,
  with NO error. The rest of the text is silently ignored. This means two
  chunks that differ only after 512 tokens would get identical embeddings.
"""

import asyncio
import re
import httpx
from app.config import settings


# ── Constants ─────────────────────────────────────────────────
# mxbai-embed-large context window is ~512 tokens
# 1 token ≈ 3 chars for code → ~1500 chars safe limit
# We use 1500 not 1536 (512×3) because some tokens map to >3 chars
MAX_EMBED_CHARS = 1500

# Output dimension of mxbai-embed-large embedding vector
# This MUST match VECTOR_DIM in redis_retriever.py for HNSW index
EMBED_DIM = 1024


def clean_for_embedding(text: str) -> str:
    """Clean text to avoid tokenizer crashes and wasted tokens.
    
    WHY: Raw source code contains null bytes (from binary files), unicode
    symbols, excessive whitespace, and control characters. These waste
    precious token budget and can crash some tokenizers.
    
    WHAT WE REMOVE:
    - Null bytes: can crash C-based tokenizers
    - Non-printable chars: replace with space (keeps word boundaries)
    - Excessive newlines: 3+ → 2 (saves tokens without losing structure)
    - Excessive spaces: 4+ → 2 (indentation usually 2 or 4, not more)
    """
    text = text.replace('\x00', '')  # Null bytes from binary file reads
    # Keep ASCII printable + newline/tab + CJK characters (for Chinese comments)
    text = re.sub(r'[^\x20-\x7E\n\t\r\u4e00-\u9fff]', ' ', text)
    text = re.sub(r'\n{3,}', '\n\n', text)  # Collapse excessive blank lines
    text = re.sub(r' {4,}', '  ', text)     # Collapse excessive spaces
    return text.strip()


def truncate_for_embedding(text: str, max_chars: int = MAX_EMBED_CHARS) -> str:
    """Truncate to fit embedding model's context window.
    
    WHY TRUNCATE BEFORE SENDING TO OLLAMA?
    Because Ollama truncates SILENTLY (Pitfall #22). If we send 3000 chars,
    Ollama embeds only the first ~1500 chars and returns a valid embedding.
    No error, no warning. The resulting embedding represents only the
    beginning of the text, which can miss important code at the end
    (like return statements or key logic).
    
    By truncating ourselves, we at least know exactly what was embedded.
    """
    text = clean_for_embedding(text)
    if len(text) <= max_chars:
        return text
    return text[:max_chars]


async def embed_text(
    text: str, model: str = "mxbai-embed-large", max_retries: int = 2
) -> list[float]:
    """Embed a single text string using Ollama's /api/embeddings endpoint.
    
    RETRY STRATEGY:
    On failure, we truncate the text to HALF its length and retry.
    Why? Common failures are:
    - Timeout (text too long → tokenization too slow)
    - Out of memory (batch processing on GPU)
    - Network hiccup (transient)
    
    Truncating on retry is better than just retrying the same text because
    the most common cause of embedding failure is text being too long or
    containing problematic characters in the tail.
    
    FALLBACK: After all retries, return a zero vector [0.0, 0.0, ...].
    This means the chunk won't match any query (cosine similarity = 0),
    but it won't crash the indexing pipeline. Better to lose one chunk
    than abort indexing 674 chunks because of one bad file.
    
    ASYNC: Uses httpx.AsyncClient which is non-blocking on the event loop.
    The HTTP connection to Ollama (localhost:11434) uses keep-alive by default.
    """
    text = truncate_for_embedding(text)

    if not text:
        return [0.0] * EMBED_DIM  # Empty text → zero vector (no match ever)

    async with httpx.AsyncClient(timeout=60) as client:
        for attempt in range(max_retries + 1):
            try:
                resp = await client.post(
                    f"{settings.ollama_base_url}/api/embeddings",
                    json={"model": model, "prompt": text},
                )
                resp.raise_for_status()
                # Ollama returns: {"embedding": [0.123, -0.456, ...]}
                return resp.json()["embedding"]
            except Exception as e:
                if attempt < max_retries:
                    # Aggressive truncation on retry — cut text in half
                    # This often fixes tokenizer/memory issues
                    text = text[: len(text) // 2]
                    # Exponential backoff: 0.3s, 0.6s
                    await asyncio.sleep(0.3 * (attempt + 1))
                else:
                    # All retries exhausted — return zero vector (graceful degradation)
                    return [0.0] * EMBED_DIM


async def embed_texts(
    texts: list[str], model: str = "mxbai-embed-large", batch_size: int = 10
) -> list[list[float]]:
    """Embed multiple texts with batch parallelism and rate limiting.
    
    WHY BATCHING?
    - asyncio.gather() sends batch_size requests concurrently to Ollama
    - Ollama processes them on its thread pool (GPU-backed)
    - Without batching, 674 chunks would take 674 sequential HTTP calls
    - With batch_size=10, only 68 rounds of parallel calls
    
    WHY RATE LIMITING (0.1s sleep)?
    - Prevents overwhelming Ollama's GPU memory
    - Ollama loads the embedding model once and keeps it in VRAM
    - Too many concurrent requests can cause OOM on GPU
    - 0.1s between batches gives Ollama time to process and free memory
    
    MEMORY: All embeddings accumulate in the `embeddings` list.
    For 674 chunks × 1024 dims × 4 bytes/float = ~2.7MB. Negligible.
    """
    embeddings = []
    for i in range(0, len(texts), batch_size):
        batch = texts[i : i + batch_size]
        # asyncio.gather() runs all embed_text() calls concurrently
        # They share the same event loop — no threads spawned
        # Each call creates its own httpx.AsyncClient connection
        batch_results = await asyncio.gather(
            *[embed_text(t, model) for t in batch]
        )
        embeddings.extend(batch_results)
        if i + batch_size < len(texts):
            await asyncio.sleep(0.1)  # Rate limit between batches
    return embeddings
