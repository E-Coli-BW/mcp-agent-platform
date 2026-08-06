from app.rag.eval.grounding import evaluate_grounding, extract_citations


def test_extracts_structured_source_citations():
    assert extract_citations("Answer\nSource: `returns-and-refunds.md:1-4`") == ["returns-and-refunds.md:1-4"]


def test_grounding_requires_retrieved_evidence_for_answerable_case():
    result = evaluate_grounding("Source: `returns.md:1-2`", ["returns.md:1-2", "shipping.md:1"], answerable=True)
    assert result["grounded"] is True
    assert result["citation_precision"] == 1.0


def test_unanswerable_case_accepts_explicit_abstention():
    result = evaluate_grounding("I couldn't find evidence; please contact support.", [], answerable=False)
    assert result["grounded"] is True
    assert result["abstained"] is True
