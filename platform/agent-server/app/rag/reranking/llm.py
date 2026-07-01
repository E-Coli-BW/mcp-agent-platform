"""LLM-as-reranker — listwise reranking using a small instruction-tuned LLM.

Why LLM rerank vs heuristic / cross-encoder?

- Heuristic (lexical bonuses): fast but blind to semantics. When the bonus
  weights are wrong they override RRF and recall collapses — brittle by nature.
- Cross-encoder (MiniLM): fast (~100ms), deterministic, semantic. Still
  pairwise — scores each (query, passage) independently, so it can't compare
  candidates against each other.
- LLM listwise: sees ALL candidates in one prompt → can reason about
  relative relevance. Slower (~500ms-1s) but gives the highest precision and
  handles edge cases (e.g., "this function looks relevant but is a deprecated
  alias — prefer the other one").

Architecture:
    Retrieve (BM25+vector+RRF, top 20) → LLM listwise rerank (top 5)

Robustness rules (learned the hard way):
1. Output INDICES, not rewritten passages. LLMs will paraphrase if you let
   them — and you'll lose your ScoredChunk metadata. Indices preserve identity.
2. Parse with multiple fallbacks. LLMs sometimes wrap output in ```json fences,
   sometimes prefix it with "Here are the rankings:". Strip aggressively.
3. Tolerate partial failures. If the LLM returns 3 indices when we asked for 5,
   fill the rest from the original ranking. Never crash the request.
4. Bound the input. Truncate each passage to a few hundred chars — we're
   ranking, not summarizing.
5. Use a timeout. LLMs can hang. Fall back to the original order on timeout.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.rag.index.retriever import ScoredChunk

logger = logging.getLogger(__name__)


# ── Prompt ────────────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = (
    "You are a relevance ranking assistant for a code search system. "
    "Given a user query and a numbered list of candidate code chunks, "
    "rank them from MOST to LEAST relevant to the query.\n"
    "\n"
    "Rules:\n"
    "1. Output ONLY a JSON array of integer indices, in ranked order.\n"
    "2. Use the indices shown in the candidates (0-based).\n"
    "3. Do NOT include explanations, markdown fences, or any other text.\n"
    "4. If you cannot rank, return [0, 1, 2, ...] in input order.\n"
    "\n"
    "Example output: [3, 0, 7, 1, 2]"
)


def _build_user_prompt(query: str, chunks: list["ScoredChunk"], max_chars: int) -> str:
    lines = [f"Query: {query}", "", "Candidates:"]
    for i, c in enumerate(chunks):
        passage = (c.content or "").strip().replace("\n", " ")
        if len(passage) > max_chars:
            passage = passage[:max_chars] + "..."
        header = f"[{i}] name={c.name!r} type={c.chunk_type} file={c.file_path}"
        lines.append(header)
        lines.append(f"    {passage}")
    lines.append("")
    lines.append("Output the ranked indices as a JSON array, e.g. [3, 0, 7, ...].")
    return "\n".join(lines)


# ── Output parsing ────────────────────────────────────────────────────────────

_JSON_ARRAY_RE = re.compile(r"\[\s*(?:-?\d+\s*,\s*)*-?\d+\s*\]")


def _parse_indices(raw: str, n: int) -> list[int]:
    """Extract a list of indices from the LLM output.

    Tolerates:
      - leading/trailing prose ("Here are the rankings: [...]")
      - markdown code fences (```json ... ```)
      - python-style list (`[0, 1, 2,]` with trailing comma)
      - duplicates and out-of-range indices (silently filtered)

    Always returns a list of length n with each index in [0, n) exactly once.
    Missing positions are filled from the original ranking.
    """
    indices: list[int] = []

    # Try strict JSON first
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        # strip fenced block
        cleaned = re.sub(r"^```[a-zA-Z]*\n?", "", cleaned)
        cleaned = re.sub(r"\n?```\s*$", "", cleaned)
        cleaned = cleaned.strip()

    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, list):
            indices = [int(x) for x in parsed if isinstance(x, (int, float))]
    except (ValueError, TypeError):
        pass

    # Regex fallback: find the first JSON-array-shaped substring
    if not indices:
        m = _JSON_ARRAY_RE.search(raw)
        if m:
            try:
                indices = [int(x) for x in json.loads(m.group(0))]
            except (ValueError, TypeError):
                indices = []

    # Dedupe + range-filter
    seen: set[int] = set()
    cleaned_indices: list[int] = []
    for i in indices:
        if 0 <= i < n and i not in seen:
            seen.add(i)
            cleaned_indices.append(i)

    # Fill missing positions from original order
    for i in range(n):
        if i not in seen:
            cleaned_indices.append(i)

    return cleaned_indices


# ── Public API ────────────────────────────────────────────────────────────────


async def llm_rerank(
    query: str,
    chunks: list["ScoredChunk"],
    top_k: int = 5,
    model_name: str | None = None,
    max_passage_chars: int | None = None,
    timeout: float | None = None,
) -> list["ScoredChunk"]:
    """Rerank chunks using a listwise LLM call.

    Returns the top_k chunks in LLM-ranked order. On any failure (no LLM
    configured, timeout, parse error), falls back gracefully to the input
    order so the RAG path NEVER crashes because of reranking.

    Args:
        query: user search query
        chunks: candidates from hybrid retrieval (typically 10-30)
        top_k: number of results to return
        model_name: LangChain model identifier; defaults to settings.rerank_llm_model
                    or settings.cheap_model
        max_passage_chars: per-passage truncation (default from settings)
        timeout: LLM call timeout in seconds (default from settings)
    """
    if not chunks:
        return []
    if len(chunks) == 1:
        return chunks[:top_k]

    from app.config import settings

    model_name = model_name or settings.rerank_llm_model or settings.cheap_model
    max_passage_chars = max_passage_chars or settings.rerank_max_passage_chars
    timeout = timeout if timeout is not None else settings.rerank_timeout_seconds

    try:
        from app.agent.graph import _create_chat_model

        model = _create_chat_model(model_name)
    except Exception as e:
        logger.warning("LLM reranker unavailable (model init failed: %s); falling back", e)
        return chunks[:top_k]

    user_prompt = _build_user_prompt(query, chunks, max_passage_chars)

    # LangChain BaseChatModel supports ainvoke with a list of messages
    from langchain_core.messages import HumanMessage, SystemMessage

    messages = [
        SystemMessage(content=_SYSTEM_PROMPT),
        HumanMessage(content=user_prompt),
    ]

    t0 = time.time()
    try:
        response = await asyncio.wait_for(model.ainvoke(messages), timeout=timeout)
    except asyncio.TimeoutError:
        logger.warning(
            "LLM reranker timed out after %.1fs (model=%s, n=%d); falling back",
            timeout, model_name, len(chunks),
        )
        return chunks[:top_k]
    except Exception as e:
        logger.warning("LLM reranker call failed: %s; falling back", e)
        return chunks[:top_k]

    elapsed_ms = (time.time() - t0) * 1000
    raw = response.content if hasattr(response, "content") else str(response)
    if not isinstance(raw, str):
        raw = str(raw)

    indices = _parse_indices(raw, len(chunks))
    reranked = [chunks[i] for i in indices]

    # Re-assign a descending score so downstream code can still sort/threshold.
    # We use 1.0 - i/N so ranks are stable and informative.
    n = len(reranked)
    for rank, chunk in enumerate(reranked):
        chunk.score = 1.0 - (rank / n)

    logger.info(
        "LLM rerank: %d → %d in %.0fms (model=%s)",
        len(chunks), top_k, elapsed_ms, model_name,
    )
    return reranked[:top_k]


def llm_rerank_sync(
    query: str,
    chunks: list["ScoredChunk"],
    top_k: int = 5,
    **kwargs,
) -> list["ScoredChunk"]:
    """Synchronous wrapper for callers outside an event loop (e.g. tests, scripts).

    If we're already inside a running loop, raise — callers must use the async
    version to avoid nested-loop pitfalls.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(llm_rerank(query, chunks, top_k=top_k, **kwargs))
    raise RuntimeError(
        "llm_rerank_sync() called from within an event loop; use `await llm_rerank(...)` instead"
    )
