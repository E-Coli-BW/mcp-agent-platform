"""Reusable retrieval and reranker benchmark metrics."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from statistics import fmean
from typing import Any, TypeAlias

BenchmarkCase: TypeAlias = tuple[str, list[str]]

BENCHMARK: list[BenchmarkCase] = [
    ("JWT authentication filter", ["JwtAuthFilter.java", "JwtAuthFilterTest.java"]),
    (
        "tenant isolation security",
        ["TenantContext.java", "TenantIsolationIntegrationTest.java"],
    ),
    ("Redis cache after transaction commit", ["CacheAfterCommitExecutor.java"]),
    ("process timeout subprocess", ["ProcessSandboxTest.java"]),
    ("file search with ripgrep", ["FileSearchService.java"]),
    ("path sandbox validation", ["PathSandbox.java", "PathSandboxTest.java"]),
    ("memory search TF-IDF", ["MemorySearchEngine.java", "MemorySearchEngineTest.java"]),
    ("optimistic locking version conflict", ["MemoryServiceTest.java"]),
    ("SSE streaming OpenAI format", ["chat.py"]),
    ("LangGraph Agent graph creation", ["builder.py", "factory.py"]),
    ("context window model view", ["context.py"]),
    ("tree-sitter code chunking", ["code.py"]),
    ("embedding Ollama mxbai", ["embedder.py"]),
    ("Redis vector HNSW search", ["redis_retriever.py"]),
    ("hybrid search BM25 RRF", ["retriever.py"]),
    ("file list recursive tree", ["definitions.py"]),
    ("workspace API open project", ["workspace.py"]),
    ("conversation sliding window Redis", ["conversation.py"]),
    (
        "FIM code completion prefix suffix",
        ["FimContextBuilder.java", "CompletionController.java"],
    ),
    ("debounce cancel Disposable", ["CompletionDebounceFilter.java"]),
]


def evaluate_retriever(
    retriever: Any,
    strategy: str = "hybrid",
    top_k_values: Sequence[int] = (5, 10),
    cases: Sequence[BenchmarkCase] = BENCHMARK,
) -> dict[str, Any]:
    """Evaluate synchronous benchmark metrics over an async retriever."""
    if strategy != "hybrid":
        raise ValueError(f"unsupported retrieval evaluation strategy: {strategy}")

    results: dict[str, Any] = {"strategy": strategy}
    with asyncio.Runner() as runner:
        for top_k in top_k_values:
            reciprocal_ranks = []
            hits = 0
            for query, expected_files in cases:
                chunks = runner.run(retriever.search(query, top_k=top_k))
                hit, reciprocal_rank, _ = ranking_outcome(
                    [Path(chunk.file_path).name for chunk in chunks],
                    expected_files,
                )
                hits += int(hit)
                reciprocal_ranks.append(reciprocal_rank)
            results[f"recall@{top_k}"] = round(hits / len(cases) * 100, 1)
            results[f"mrr@{top_k}"] = round(fmean(reciprocal_ranks), 3)
    return results


def evaluate_with_reranker(
    retriever: Any,
    rerank_fn: Callable[..., list[Any]],
    label: str,
    top_k: int = 5,
    cases: Sequence[BenchmarkCase] = BENCHMARK,
) -> dict[str, Any]:
    """Evaluate one synchronous reranker over a fixed candidate set."""
    hits = 0
    reciprocal_ranks = []
    total_latency_ms = 0.0
    per_query = []
    with asyncio.Runner() as runner:
        for query, expected_files in cases:
            chunks = runner.run(retriever.search(query, top_k=20))
            started_at = time.perf_counter()
            reranked = rerank_fn(query, list(chunks), top_k=top_k)
            latency_ms = (time.perf_counter() - started_at) * 1000
            total_latency_ms += latency_ms
            result_files = [Path(chunk.file_path).name for chunk in reranked]
            hit, reciprocal_rank, rank = ranking_outcome(
                result_files,
                expected_files,
            )
            hits += int(hit)
            reciprocal_ranks.append(reciprocal_rank)
            per_query.append(
                {
                    "query": query[:50],
                    "expected": expected_files[0],
                    "got": result_files[:3],
                    "hit": hit,
                    "rank": rank,
                    "latency_ms": round(latency_ms, 1),
                }
            )
    return {
        "label": label,
        "recall@5": round(hits / len(cases) * 100, 1),
        "mrr": round(fmean(reciprocal_ranks), 3),
        "avg_latency_ms": round(total_latency_ms / len(cases), 1),
        "total_latency_ms": round(total_latency_ms, 1),
        "per_query": per_query,
    }


def evaluate_without_reranker(
    retriever: Any,
    top_k: int = 5,
    cases: Sequence[BenchmarkCase] = BENCHMARK,
) -> dict[str, Any]:
    """Evaluate raw retrieval order without a reranking pass."""
    hits = 0
    reciprocal_ranks = []
    with asyncio.Runner() as runner:
        for query, expected_files in cases:
            chunks = runner.run(retriever.search(query, top_k=top_k))
            hit, reciprocal_rank, _ = ranking_outcome(
                [Path(chunk.file_path).name for chunk in chunks],
                expected_files,
            )
            hits += int(hit)
            reciprocal_ranks.append(reciprocal_rank)
    return {
        "label": "No rerank (RRF only)",
        "recall@5": round(hits / len(cases) * 100, 1),
        "mrr": round(fmean(reciprocal_ranks), 3),
        "avg_latency_ms": 0.0,
    }


def ranking_outcome(
    result_files: Sequence[str],
    expected_files: Sequence[str],
) -> tuple[bool, float, int | None]:
    """Return hit, reciprocal rank, and best one-based rank."""
    ranks = [
        result_files.index(expected) + 1
        for expected in expected_files
        if expected in result_files
    ]
    if not ranks:
        return False, 0.0, None
    best_rank = min(ranks)
    return True, 1.0 / best_rank, best_rank
