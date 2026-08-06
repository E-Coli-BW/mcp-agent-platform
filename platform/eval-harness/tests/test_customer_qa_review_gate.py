import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ci.customer_qa_review_gate import main
from ci.generate_customer_qa_dataset import build_cases, validate_cases


def test_pending_candidates_pass_schema_but_not_review_gate(tmp_path, monkeypatch, capsys):
    cases = build_cases()
    assert validate_cases(cases) == []
    assert validate_cases(cases, require_reviewed=True)
    path = tmp_path / "cases.json"
    path.write_text(json.dumps(cases))
    monkeypatch.setattr(sys, "argv", ["gate", str(path)])
    assert main() == 0
    assert json.loads(capsys.readouterr().out)["cases"] == 300
