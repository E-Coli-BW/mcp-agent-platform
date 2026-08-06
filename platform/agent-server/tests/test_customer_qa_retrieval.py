from app.rag.eval.customer_qa import score_results


def test_retrieval_metrics_include_hard_negative_leakage():
    cases = [
        {"id": "a", "expected_sources": ["returns.md"], "hard_negative_sources": ["shipping.md"]},
        {"id": "b", "expected_sources": [], "hard_negative_sources": ["private.md"]},
    ]
    result = score_results(cases, {"a": ["shipping.md", "returns.md"], "b": ["private.md"]}, top_k=2)
    assert result == {"cases": 2, "recall_at_k": 0.5, "mrr_at_k": 0.25, "hard_negative_leak_rate": 1.0, "hard_negative_at_one": 1.0, "target_at_one": 0.0, "top_k": 2}
