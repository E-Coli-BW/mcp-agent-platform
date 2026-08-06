from scripts.customer_qa_reranker_benchmark import CASES, PASSAGES
from scripts.customer_qa_reranker_benchmark import evaluate
from app.rag.index.retriever import ScoredChunk


def test_benchmark_has_discriminative_baseline_cases():
    """A benchmark whose baseline is perfect cannot measure reranking lift."""
    assert len(PASSAGES) >= 6
    assert len(CASES) >= 6
    baseline = evaluate(lambda _q, candidates, top_k: candidates[:top_k], "none")
    assert baseline["mrr"] < 1.0
    assert any(case["rank"] != 1 for case in baseline["per_case"])


def test_reranker_benchmark_uses_same_fixed_candidate_set():
    assert len(CASES) >= 6
    assert set(expected for _, expected in CASES) <= set(PASSAGES)
