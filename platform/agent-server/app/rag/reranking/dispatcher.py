"""Reranker dispatcher — chooses among LLM / cross-encoder / heuristic strategies.

Quality vs latency trade-off:

    Strategy       Quality      Latency      Notes
    ────────────────────────────────────────────────────────────────────
    none           baseline    0ms          RRF passthrough — baseline
    heuristic      ≈ baseline  <5ms         Lexical bonuses; brittle (see below)
    cross_encoder  > heuristic ~100ms       MiniLM pairwise; deterministic
    llm            highest     ~500ms-1s    Listwise; sees all candidates together

The `rerank()` function is the single public entry point. It picks a strategy
based on `settings.rerank_strategy` and falls back gracefully if a backend is
unavailable (model not installed, network down, etc.).

History — why we keep the heuristic at all:
- Original implementation had oversized bonuses (0.1-0.5) that crushed RRF
  ranking and tanked recall. Scaling bonuses to ~20% of the median base score
  fixed it. It's now a safe lexical-boost fallback, but it has a HARD ceiling
  at recall ≈ baseline. Use cross_encoder or llm in production.

Benchmarking:
- `eval/benchmark_rerankers.py` covers none / heuristic / cross_encoder over a
  20-query set (recall@5 + MRR + latency). The LLM strategy isn't wired into
  that harness, so the quality ordering above is directional for that arm, not
  a measured number. Absolute numbers depend on the index and query set.
- Strategy is a settings switch for per-workload A/B: interactive tool-call RAG
  defaults to cross_encoder (latency-sensitive); offline grading can afford the
  LLM listwise pass.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.rag.index.retriever import ScoredChunk

logger = logging.getLogger(__name__)


# ── Strategy: heuristic (legacy, kept as fallback) ────────────────────────────


def heuristic_rerank(
    query: str, chunks: list["ScoredChunk"], top_k: int = 5
) -> list["ScoredChunk"]:
    """Rerank using lexical bonuses on top of RRF scores.

    Bonuses are scaled to ~20% of median base score to avoid overriding the
    retrieval ranking (RRF scores are small: ~0.01-0.03). See module docstring
    for the recall-killing bug we fixed.
    """
    query_terms = set(re.split(r"\W+", query.lower()))
    query_terms.discard("")

    if not chunks:
        return []

    base_scores = [c.score for c in chunks if c.score > 0]
    median_score = sorted(base_scores)[len(base_scores) // 2] if base_scores else 0.01
    scale = median_score * 0.2

    for chunk in chunks:
        bonus = 0.0

        # 1. Name match bonus (strong signal)
        if chunk.name:
            name_lower = chunk.name.lower()
            name_terms = set(re.split(r"[_\W]+", name_lower))
            overlap = query_terms & name_terms
            if overlap:
                bonus += 2.0 * (len(overlap) / max(len(query_terms), 1))

        # 2. Exact substring match
        content_lower = chunk.content.lower()
        query_lower = query.lower()
        if query_lower in content_lower:
            bonus += 1.5
        elif any(term in content_lower for term in query_terms if len(term) > 3):
            bonus += 0.5

        # 3. Chunk type preference
        type_bonus = {"function": 0.5, "method": 0.5, "class": 0.25, "module": 0.0}
        bonus += type_bonus.get(chunk.chunk_type, 0.0)

        # 4. Brevity bonus (prefer focused chunks over huge ones)
        content_len = len(chunk.content)
        if content_len < 500:
            bonus += 0.25
        elif content_len > 2000:
            bonus -= 0.25

        chunk.score = chunk.score + bonus * scale

    chunks.sort(key=lambda c: c.score, reverse=True)
    return chunks[:top_k]


# ── Dispatcher ────────────────────────────────────────────────────────────────


def _resolve_strategy(requested: str) -> str:
    """Map 'auto' to the best available concrete strategy."""
    requested = (requested or "auto").lower()
    if requested != "auto":
        return requested
    # Auto: prefer cross_encoder if sentence-transformers is installed; else heuristic.
    try:
        import sentence_transformers  # noqa: F401

        return "cross_encoder"
    except ImportError:
        return "heuristic"


async def arerank(
    query: str,
    chunks: list["ScoredChunk"],
    top_k: int = 5,
    strategy: str | None = None,
) -> list["ScoredChunk"]:
    """Async rerank dispatcher. Use this from async code (FastAPI handlers,
    LangGraph nodes). Always falls back to a safe strategy on failure.
    """
    if not chunks:
        return []

    from app.config import settings

    strat = _resolve_strategy(strategy or settings.rerank_strategy)

    try:
        if strat == "none":
            return chunks[:top_k]

        if strat == "llm":
            from app.rag.reranking.llm import llm_rerank

            return await llm_rerank(query, chunks, top_k=top_k)

        if strat == "cross_encoder":
            from app.rag.reranking.cross_encoder import cross_encoder_rerank

            # cross_encoder_rerank is sync + CPU-bound; offload from event loop
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(
                None, cross_encoder_rerank, query, chunks, top_k
            )

        # default / legacy
        return heuristic_rerank(query, chunks, top_k=top_k)

    except Exception as e:
        logger.warning(
            "Reranker strategy %r failed (%s); falling back to heuristic", strat, e
        )
        return heuristic_rerank(query, chunks, top_k=top_k)


def rerank(
    query: str,
    chunks: list["ScoredChunk"],
    top_k: int = 5,
    strategy: str | None = None,
) -> list["ScoredChunk"]:
    """Synchronous rerank entry point.

    BACKWARD-COMPATIBLE: existing callers that did ``rerank(query, chunks, top_k)``
    keep working — they now go through the dispatcher and get whatever strategy
    ``settings.rerank_strategy`` points at (default ``auto`` → cross_encoder if
    available, else heuristic).

    For async contexts, prefer ``await arerank(...)``. Calling this function from
    inside a running event loop with strategy=``llm`` raises — use the async
    version instead.
    """
    if not chunks:
        return []

    from app.config import settings

    strat = _resolve_strategy(strategy or settings.rerank_strategy)

    try:
        if strat == "none":
            return chunks[:top_k]

        if strat == "cross_encoder":
            from app.rag.reranking.cross_encoder import cross_encoder_rerank

            return cross_encoder_rerank(query, chunks, top_k=top_k)

        if strat == "llm":
            # Sync entry point for LLM rerank: only safe when no loop is running.
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                from app.rag.reranking.llm import llm_rerank

                return asyncio.run(llm_rerank(query, chunks, top_k=top_k))
            logger.warning(
                "rerank(strategy='llm') called from a running event loop; "
                "use `await arerank(...)`. Falling back to heuristic."
            )
            return heuristic_rerank(query, chunks, top_k=top_k)

        return heuristic_rerank(query, chunks, top_k=top_k)

    except Exception as e:
        logger.warning(
            "Reranker strategy %r failed (%s); falling back to heuristic", strat, e
        )
        return heuristic_rerank(query, chunks, top_k=top_k)


__all__ = ["rerank", "arerank", "heuristic_rerank"]
