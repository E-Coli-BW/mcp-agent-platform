from types import SimpleNamespace

from app.rag.retrieval_pipeline import apply_domain_intent_prior, classify_customer_qa_intent, deduplicate_and_diversify, expand_parent_context, filter_by_intent, rewrite_query


def _result(path, score, parent=None, content="child"):
    return SimpleNamespace(file_path=path, start_line=1, score=score, content=content, metadata={"parent_id": parent} if parent else {})


def test_rewrite_preserves_identifiers_and_removes_polite_prefix():
    assert rewrite_query("Please check order 12345") == "check order 12345"


def test_diversification_deduplicates_parent_chunks():
    results = deduplicate_and_diversify([_result("a", 1.0, "p"), _result("a", 0.9, "p"), _result("b", 0.8)], 2)
    assert [item.file_path for item in results] == ["a", "b"]


def test_parent_context_is_expanded_only_when_declared():
    result = expand_parent_context(_result("a", 1.0, "parent"), {"parent": "section context"})
    assert result.content.startswith("section context")


def test_domain_intent_prior_prefers_matching_support_source():
    results = apply_domain_intent_prior("where is my package", [_result("returns-and-refunds.md", 1.0), _result("shipping-status.md", 0.95)])
    assert results[0].file_path == "shipping-status.md"


def test_intent_filter_only_applies_to_high_confidence_queries():
    intent, confidence = classify_customer_qa_intent("where is my package")
    assert intent == "shipping_status" and confidence == 1.0
    results = filter_by_intent("where is my package", [_result("returns-and-refunds.md", 1), _result("shipping-status.md", .8)])
    assert [item.file_path for item in results] == ["shipping-status.md"]
