import pytest

from app.runtime.checkpoint import InvalidCheckpointTransition, SQLiteCheckpointStore


def test_checkpoint_worker_transitions_are_restart_visible(tmp_path):
    store = SQLiteCheckpointStore(tmp_path / "runtime.sqlite3")
    store.save(run_id="run-1", step_seq=1, state="approved", tool_name="code_shell", payload={"command": "echo ok"})
    assert store.claim("run-1", owner="worker-a") is not None
    assert store.transition("run-1", state="executing").state == "executing"
    restarted = SQLiteCheckpointStore(tmp_path / "runtime.sqlite3")
    assert restarted.get("run-1").state == "executing"
    assert restarted.transition("run-1", state="completed", payload={"result": "ok"}).payload["result"] == "ok"


def test_terminal_checkpoint_cannot_regress_to_approved(tmp_path):
    store = SQLiteCheckpointStore(tmp_path / "runtime.sqlite3")
    store.save(run_id="run-1", step_seq=1, state="approved")
    store.transition("run-1", state="executing")
    store.transition("run-1", state="completed", payload={"result": "ok"})

    with pytest.raises(InvalidCheckpointTransition):
        store.transition("run-1", state="approved")

    assert store.get("run-1").state == "completed"
