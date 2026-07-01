"""Rerank — strategy dispatcher and individual reranker backends.

Strategies (quality ↑, latency ↑):
    none           — pass through RRF
    heuristic      — lexical-bonus boosting (fast, brittle)
    cross_encoder  — MiniLM pairwise scoring (~100ms)
    llm            — listwise LLM rerank (~500ms-1s, highest precision)
    learned        — online-learned weights from agent feedback

Public entry points:
    from app.rag.rerank import rerank, arerank, heuristic_rerank
    from app.rag.rerank import cross_encoder_rerank, llm_rerank, get_learned_reranker
"""

from app.rag.reranking.dispatcher import (
    arerank,
    heuristic_rerank,
    rerank,
)
from app.rag.reranking.cross_encoder import cross_encoder_rerank
from app.rag.reranking.llm import llm_rerank
from app.rag.reranking.learned import get_learned_reranker

__all__ = [
    "rerank",
    "arerank",
    "heuristic_rerank",
    "cross_encoder_rerank",
    "llm_rerank",
    "get_learned_reranker",
]
