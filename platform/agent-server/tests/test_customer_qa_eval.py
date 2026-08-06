from pathlib import Path


def test_customer_qa_eval_contract_exists():
    assert (Path(__file__).parents[1] / "scripts" / "customer_qa_eval.py").exists()
