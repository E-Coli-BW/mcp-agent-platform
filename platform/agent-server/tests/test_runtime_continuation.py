from app.runtime.checkpoint import SQLiteCheckpointStore
from app.runtime.continuation import ContinuationWorker


def test_continuation_worker_completes_once(tmp_path):
    store = SQLiteCheckpointStore(tmp_path / "runtime.sqlite3")
    store.save(run_id="run-1", step_seq=1, state="approved", tool_name="code_shell")
    calls = []
    worker = ContinuationWorker(store, "worker-a")
    result = worker.run_once("run-1", lambda checkpoint: calls.append(checkpoint.run_id) or "ok")
    assert result.state == "completed"
    assert calls == ["run-1"]
    assert worker.run_once("run-1", lambda _: calls.append("duplicate")) .state == "completed"
    assert calls == ["run-1"]


def test_continuation_worker_persists_failure(tmp_path):
    store = SQLiteCheckpointStore(tmp_path / "runtime.sqlite3")
    store.save(run_id="run-1", step_seq=1, state="approved")
    result = ContinuationWorker(store, "worker-a").run_once("run-1", lambda _: 1 / 0)
    assert result.state == "failed"
    assert result.payload["error_class"] == "ZeroDivisionError"


def test_continuation_worker_does_not_replay_unknown_executing_outcome(tmp_path):
    store = SQLiteCheckpointStore(tmp_path / "runtime.sqlite3")
    store.save(run_id="run-1", step_seq=1, state="approved", tool_name="file_write")
    store.transition("run-1", state="executing")
    calls = []

    result = ContinuationWorker(store, "worker-b").run_once(
        "run-1", lambda _: calls.append("duplicate")
    )

    assert result.state == "executing"
    assert calls == []
