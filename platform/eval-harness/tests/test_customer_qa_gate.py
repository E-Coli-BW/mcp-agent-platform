import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ci.customer_qa_gate import main


def _summary(tmp_path, grounded=(1, 1)):
    path = tmp_path / "summary.json"
    path.write_text(json.dumps({
        "agent_id": "customer-qa-agent",
        "run_type": "qa",
        "pass_rate": 1.0,
        "by_scorer": {"answer_grounded": {"passed": grounded[0], "total": grounded[1]}},
    }))
    return path


def test_gate_passes(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["gate", str(_summary(tmp_path))])
    assert main() == 0
    assert json.loads(capsys.readouterr().out)["passed"] is True


def test_gate_rejects_low_grounding(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["gate", str(_summary(tmp_path, grounded=(1, 2)))])
    assert main() == 1
    result = json.loads(capsys.readouterr().out)
    assert result["passed"] is False
    assert "answer_grounded" in result["failures"][0]
