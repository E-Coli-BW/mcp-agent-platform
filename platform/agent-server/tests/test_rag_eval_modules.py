from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.rag.eval import benchmark_compression, benchmark_rerankers
from app.rag.eval.ranking import evaluate_retriever
from app.rag.reranking.dispatcher import heuristic_rerank


def test_head_tail_compression_and_semantic_score_are_stable():
    content = "def alpha():\n    return 1\n" + "x" * 2000 + "\nclass Omega:\n    pass\n"

    compressed = benchmark_compression.head_tail_compress(content, max_chars=1500)
    scores = benchmark_compression.score_semantic_preservation(content, compressed)

    assert len(compressed) < len(content)
    assert "def alpha" in compressed
    assert "class Omega" in compressed
    assert 0 <= scores["total"] <= 100


class FakeRetriever:
    async def search(self, query, top_k):
        return [
            SimpleNamespace(file_path="src/expected.py", score=0.5),
            SimpleNamespace(file_path="src/other.py", score=0.4),
        ][:top_k]


def test_reranker_benchmark_aggregates_recall_and_mrr(monkeypatch):
    monkeypatch.setattr(
        benchmark_rerankers,
        "BENCHMARK",
        [("query", ["expected.py"])],
    )

    result = benchmark_rerankers._evaluate_with_reranker(
        FakeRetriever(),
        lambda _query, chunks, top_k: chunks[:top_k],
        "test",
        top_k=2,
    )
    baseline = benchmark_rerankers._evaluate_no_rerank(FakeRetriever(), top_k=2)

    assert result["recall@5"] == 100.0
    assert result["mrr"] == 1.0
    assert baseline["recall@5"] == 100.0
    assert baseline["mrr"] == 1.0


def test_retrieval_evaluation_rejects_unsupported_strategy():
    with pytest.raises(ValueError, match="unsupported retrieval evaluation strategy"):
        evaluate_retriever(FakeRetriever(), strategy="vector")


def test_reranker_benchmark_uses_explicit_heuristic_owner():
    assert benchmark_rerankers.heuristic_rerank is heuristic_rerank
