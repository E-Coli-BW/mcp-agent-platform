"""Cross-encoder reranker — uses a lightweight model to score (query, passage) pairs.

Unlike bi-encoders (embed query + embed passage separately), cross-encoders
process the PAIR together, giving much better relevance scores at the cost
of being slower (can't pre-compute passage embeddings).

This uses `cross-encoder/ms-marco-MiniLM-L-6-v2` (~80MB, runs on CPU in <100ms
for 10 passages). For CJK-heavy codebases, consider `BAAI/bge-reranker-base`.

Architecture:
    Retrieve (BM25+vector+RRF, top 20) → Cross-encoder rerank (top 5)
    
    The cross-encoder sees both query and passage text, producing a single
    relevance score. This is fundamentally more accurate than heuristic
    scoring because it captures semantic relationships between query and passage.

Usage:
    from app.rag.reranking.cross_encoder import cross_encoder_rerank
    reranked = cross_encoder_rerank(query, chunks, top_k=5)
"""

import logging
import time
from functools import lru_cache

from app.rag.index.retriever import ScoredChunk

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _load_model():
    """Lazy-load the cross-encoder model (downloaded on first use, ~80MB).
    
    Uses lru_cache to load only once per process lifetime.
    The model runs on CPU — no GPU required.
    """
    try:
        from sentence_transformers import CrossEncoder
        model_name = "cross-encoder/ms-marco-MiniLM-L-6-v2"
        logger.info("Loading cross-encoder model: %s", model_name)
        t0 = time.time()
        model = CrossEncoder(model_name)
        logger.info("Cross-encoder loaded in %.1fs", time.time() - t0)
        return model
    except ImportError:
        logger.warning(
            "sentence-transformers not installed. "
            "Install with: pip install sentence-transformers"
        )
        return None
    except Exception as e:
        logger.error("Failed to load cross-encoder: %s", e)
        return None


def cross_encoder_rerank(
    query: str,
    chunks: list[ScoredChunk],
    top_k: int = 5,
) -> list[ScoredChunk]:
    """Rerank chunks using a cross-encoder model.
    
    Falls back to returning chunks unchanged if the model can't be loaded.
    
    Args:
        query: The user's search query
        chunks: Pre-retrieved chunks from hybrid search (typically top 10-20)
        top_k: Number of results to return after reranking
        
    Returns:
        Top-k chunks reranked by cross-encoder relevance score
    """
    if not chunks:
        return []
    
    model = _load_model()
    if model is None:
        logger.warning("Cross-encoder unavailable, returning original ranking")
        return chunks[:top_k]
    
    # Build (query, passage) pairs for the cross-encoder
    # Use chunk name + content for richer context
    pairs = []
    for chunk in chunks:
        passage = f"{chunk.name} ({chunk.chunk_type})\n{chunk.content[:1000]}"
        pairs.append((query, passage))
    
    # Score all pairs in a single batch (efficient)
    t0 = time.time()
    scores = model.predict(pairs)
    elapsed_ms = (time.time() - t0) * 1000
    logger.info(
        "Cross-encoder scored %d chunks in %.0fms (%.1fms/chunk)",
        len(chunks), elapsed_ms, elapsed_ms / len(chunks)
    )
    
    # Assign cross-encoder scores and sort
    for chunk, score in zip(chunks, scores):
        chunk.score = float(score)
    
    chunks.sort(key=lambda c: c.score, reverse=True)
    return chunks[:top_k]
