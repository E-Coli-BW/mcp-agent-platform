from ci.generate_customer_qa_dataset import build_cases


def test_dataset_has_auditable_case_schema_and_target_size():
    cases = build_cases()
    assert len(cases) == 300
    assert len({case["id"] for case in cases}) == len(cases)
    for case in cases:
        assert case["version"]
        assert case["question"]
        assert case["review"]["status"] == "synthetic_pending"
        assert case["hard_negatives"]
        assert case["provenance"]["source_urls"]
        assert isinstance(case["answerable"], bool)
