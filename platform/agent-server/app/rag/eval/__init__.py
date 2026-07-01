"""Eval — RAG retrieval & reranker & compression benchmarks.

Modules:
    dataset                — BENCHMARK pairs + recall@K runner
    benchmark_rerankers    — compares reranker strategies on the dataset
    benchmark_compression  — AST vs head+tail compression comparison

Public entry points:
    from app.rag.eval import BENCHMARK
"""

from app.rag.eval.dataset import BENCHMARK

__all__ = ["BENCHMARK"]
