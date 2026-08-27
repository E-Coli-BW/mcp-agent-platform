import sqlite3
import time

import pytest

from app.runtime.checkpoint import InvalidCheckpointTransition, SQLiteCheckpointStore


def _payload(resume_at: float, attempts_failed: int = 1) -> dict:
    return {
        "kind": "model_retry",
        "session_id": "tenant:session",
        "agent_id": "coding-agent",
        "prompt_version": "v2",
        "attempts_failed": attempts_failed,
        "max_retries": 2,
        "resume_at": resume_at,
        "failure": {"code": "SERVER", "message": "temporary"},
    }


def test_due_waiting_retry_is_restart_visible_and_claimable(tmp_path):
    database = tmp_path / "runtime.sqlite3"
    first = SQLiteCheckpointStore(database)
    first.save(
        run_id="run-1",
        step_seq=1,
        state="model_retry_waiting",
        payload=_payload(time.time() - 1),
    )

    restarted = SQLiteCheckpointStore(database)
    recoverable = restarted.list_recoverable()

    assert [checkpoint.run_id for checkpoint in recoverable] == ["run-1"]
    claimed = restarted.claim(
        "run-1",
        owner="worker-b",
        expected_state="model_retry_waiting",
    )
    assert claimed is not None
    assert claimed.lease_owner == "worker-b"


def test_future_retry_wait_is_not_recoverable(tmp_path):
    store = SQLiteCheckpointStore(tmp_path / "runtime.sqlite3")
    store.save(
        run_id="run-future",
        step_seq=1,
        state="model_retry_waiting",
        payload=_payload(time.time() + 60),
    )

    assert store.list_recoverable() == []


def test_active_execution_requires_lease_expiry_before_recovery(tmp_path):
    store = SQLiteCheckpointStore(tmp_path / "runtime.sqlite3")
    store.save(
        run_id="run-active",
        step_seq=1,
        state="model_retry_executing",
        payload=_payload(0, attempts_failed=0),
    )
    assert store.claim(
        "run-active",
        owner="worker-a",
        lease_seconds=60,
        expected_state="model_retry_executing",
    ) is not None
    assert store.list_recoverable() == []

    with sqlite3.connect(store.database_path) as connection:
        connection.execute(
            "UPDATE agent_run_checkpoints SET lease_expires_at = ? WHERE run_id = ?",
            (time.time() - 1, "run-active"),
        )

    assert [item.run_id for item in store.list_recoverable()] == ["run-active"]


def test_retry_lifecycle_keeps_owner_lease_while_waiting(tmp_path):
    store = SQLiteCheckpointStore(tmp_path / "runtime.sqlite3")
    store.save(
        run_id="run-1",
        step_seq=1,
        state="model_retry_executing",
        payload=_payload(0, attempts_failed=0),
    )
    assert store.claim(
        "run-1",
        owner="worker-a",
        expected_state="model_retry_executing",
    ) is not None

    waiting = store.transition(
        "run-1",
        state="model_retry_waiting",
        payload=_payload(time.time() - 1),
    )
    assert waiting is not None
    assert waiting.lease_owner == "worker-a"
    assert waiting.lease_expires_at is not None

    assert store.claim(
        "run-1",
        owner="worker-b",
        expected_state="model_retry_waiting",
    ) is None
    assert store.renew_lease(
        "run-1",
        owner="worker-a",
    ) is not None
    executing = store.transition("run-1", state="model_retry_executing")
    assert executing is not None
    assert executing.payload["attempts_failed"] == 1
    assert store.transition("run-1", state="completed", clear_lease=True).state == "completed"


def test_expired_waiting_lease_can_be_claimed_by_recovery_worker(tmp_path):
    store = SQLiteCheckpointStore(tmp_path / "runtime.sqlite3")
    store.save(
        run_id="run-crashed",
        step_seq=1,
        state="model_retry_waiting",
        payload=_payload(time.time() - 1),
    )
    assert store.claim(
        "run-crashed",
        owner="dead-worker",
        expected_state="model_retry_waiting",
    ) is not None
    with sqlite3.connect(store.database_path) as connection:
        connection.execute(
            "UPDATE agent_run_checkpoints SET lease_expires_at = ? WHERE run_id = ?",
            (time.time() - 1, "run-crashed"),
        )

    assert store.claim(
        "run-crashed",
        owner="recovery-worker",
        expected_state="model_retry_waiting",
    ) is not None
    assert store.get("run-crashed").lease_owner == "recovery-worker"


def test_expired_owner_cannot_transition_after_recovery_worker_claims(tmp_path):
    store = SQLiteCheckpointStore(tmp_path / "runtime.sqlite3")
    store.save(
        run_id="run-fenced",
        step_seq=1,
        state="model_retry_executing",
        payload=_payload(0),
    )
    assert store.claim(
        "run-fenced",
        owner="worker-a",
        expected_state="model_retry_executing",
    ) is not None
    with sqlite3.connect(store.database_path) as connection:
        connection.execute(
            "UPDATE agent_run_checkpoints SET lease_expires_at = ? WHERE run_id = ?",
            (time.time() - 1, "run-fenced"),
        )
    assert store.claim(
        "run-fenced",
        owner="worker-b",
        expected_state="model_retry_executing",
    ) is not None

    assert store.transition(
        "run-fenced",
        state="completed",
        clear_lease=True,
        owner="worker-a",
    ) is None
    assert store.get("run-fenced").state == "model_retry_executing"
    assert store.transition(
        "run-fenced",
        state="completed",
        clear_lease=True,
        owner="worker-b",
    ).state == "completed"


def test_in_flight_crash_can_only_move_to_reconciliation(tmp_path):
    store = SQLiteCheckpointStore(tmp_path / "runtime.sqlite3")
    store.save(
        run_id="run-unknown",
        step_seq=1,
        state="model_retry_executing",
        payload=_payload(0),
    )

    reconciled = store.transition(
        "run-unknown",
        state="reconciliation_required",
        payload={"reason": "provider outcome is unknown"},
        clear_lease=True,
    )

    assert reconciled is not None
    assert reconciled.state == "reconciliation_required"
    with pytest.raises(InvalidCheckpointTransition):
        store.transition("run-unknown", state="model_retry_waiting")