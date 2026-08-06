import json
import subprocess
import sys


def test_two_process_approval_recovery_script(tmp_path):
    result = subprocess.run(
        [sys.executable, "scripts/runtime_approval_recovery.py", "--runtime-db", str(tmp_path / "runtime.sqlite3")],
        check=True,
        capture_output=True,
        text=True,
    )
    report = json.loads(result.stdout)
    assert report["passed"] is True
    assert report["resume"]["state"] == "completed"
    assert report["resume"]["side_effects"] == ["printf recovery"]
