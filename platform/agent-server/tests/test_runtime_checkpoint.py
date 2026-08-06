from app.runtime.checkpoint import SQLiteCheckpointStore

def test_checkpoint_survives_restart_and_lease_is_exclusive(tmp_path):
    path = tmp_path / "runtime.sqlite3"
    first = SQLiteCheckpointStore(path)
    first.save(run_id="run-1", step_seq=2, state="waiting_approval", tool_name="code_shell", approval_id="a-1", payload={"command": "echo ok"})
    second = SQLiteCheckpointStore(path)
    restored = second.get("run-1")
    assert restored.state == "waiting_approval"
    assert [item.run_id for item in second.list_recoverable()] == ["run-1"]
    assert second.claim("run-1", owner="worker-a") is not None
    assert second.claim("run-1", owner="worker-a") is None
    assert second.claim("run-1", owner="worker-b") is None
