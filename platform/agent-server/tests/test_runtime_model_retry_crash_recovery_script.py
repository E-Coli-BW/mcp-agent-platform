import json
import subprocess
import sys


def test_two_process_model_retry_crash_recovery_script():
    completed = subprocess.run(
        [sys.executable, "scripts/runtime_model_retry_crash_recovery.py"],
        check=True,
        capture_output=True,
        text=True,
        timeout=45,
    )
    report = json.loads(completed.stdout)

    assert report["passed"] is True
    assert report["attempts_failed_before_crash"] == 1
    assert report["provider_calls"] == 2
    assert report["recoverable_count"] == 1
    assert report["checkpoint_state"] == "completed"
    assert report["response"] == "RECOVERED"
    assert report["run_status"] == "completed"
    assert report["terminal_reason"] == "model_retry_recovered"