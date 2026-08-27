import asyncio
import time

import pytest

from app.api import runtime_control
from app.auth.middleware import AuthContext
from app.runtime import RunStatus, get_run_recorder, reset_run_recorder_for_tests
from app.runtime.checkpoint import SQLiteCheckpointStore
from app.runtime.model_retry_recovery import ModelRetryResumeResult


@pytest.fixture(autouse=True)
def reset_recorder():
    asyncio.run(reset_run_recorder_for_tests())
    yield
    asyncio.run(reset_run_recorder_for_tests())


def _auth() -> AuthContext:
    return AuthContext(user_id="operator", tenant_id="tenant-a", permissions=[])


def _start_run(run_id: str) -> None:
    asyncio.run(
        get_run_recorder().start_run(
            run_id=run_id,
            request_id=f"request-{run_id}",
            tenant_id="tenant-a",
            session_id="tenant-a:session",
            model="coding-agent",
        )
    )


def _payload(resume_at: float) -> dict:
    return {
        "kind": "model_retry",
        "session_id": "tenant-a:session",
        "agent_id": "coding-agent",
        "prompt_version": "v2",
        "request_fingerprint": "sha256:test",
        "attempts_failed": 1,
        "max_retries": 2,
        "resume_at": resume_at,
    }


def test_resume_due_model_retry_once(tmp_path, monkeypatch):
    checkpoints = SQLiteCheckpointStore(tmp_path / "runtime.sqlite3")
    _start_run("run-retry")
    checkpoints.save(
        run_id="run-retry",
        step_seq=1,
        state="model_retry_waiting",
        tool_name="__model__",
        payload=_payload(time.time() - 1),
    )
    monkeypatch.setattr(runtime_control, "get_checkpoint_store", lambda: checkpoints)

    async def fake_resume(**kwargs):
        owner = kwargs["checkpoint"].lease_owner
        assert owner is not None and owner.startswith("model:")
        completed = checkpoints.transition(
            "run-retry",
            state="completed",
            clear_lease=True,
            owner=owner,
        )
        return ModelRetryResumeResult("recovered", completed)

    monkeypatch.setattr(
        "app.runtime.model_retry_recovery.resume_model_retry_run",
        fake_resume,
    )

    first = asyncio.run(runtime_control.resume_run("run-retry", _auth()))
    second = asyncio.run(runtime_control.resume_run("run-retry", _auth()))

    assert first["accepted"] is True
    assert first["response"] == "recovered"
    assert first["checkpoint"]["state"] == "completed"
    assert second["accepted"] is False
    assert second["reason"] == "checkpoint is not approved"


def test_resume_rejects_future_backoff(tmp_path, monkeypatch):
    checkpoints = SQLiteCheckpointStore(tmp_path / "runtime.sqlite3")
    _start_run("run-future")
    checkpoints.save(
        run_id="run-future",
        step_seq=1,
        state="model_retry_waiting",
        tool_name="__model__",
        payload=_payload(time.time() + 60),
    )
    monkeypatch.setattr(runtime_control, "get_checkpoint_store", lambda: checkpoints)

    response = asyncio.run(runtime_control.resume_run("run-future", _auth()))

    assert response["accepted"] is False
    assert response["reason"] == "model retry backoff is still pending"
    assert response["retry_after_ms"] > 0
    assert checkpoints.get("run-future").state == "model_retry_waiting"


def test_due_retry_waits_for_active_lease_expiry(tmp_path, monkeypatch):
    checkpoints = SQLiteCheckpointStore(tmp_path / "runtime.sqlite3")
    _start_run("run-leased")
    checkpoints.save(
        run_id="run-leased",
        step_seq=1,
        state="model_retry_waiting",
        tool_name="__model__",
        payload=_payload(time.time() - 1),
    )
    assert checkpoints.claim(
        "run-leased",
        owner="active-worker",
        lease_seconds=30,
        expected_state="model_retry_waiting",
    ) is not None
    monkeypatch.setattr(runtime_control, "get_checkpoint_store", lambda: checkpoints)

    response = asyncio.run(runtime_control.resume_run("run-leased", _auth()))

    assert response["accepted"] is False
    assert response["reason"] == "lease held"
    assert 0 < response["retry_after_ms"] <= 30_000
    assert checkpoints.get("run-leased").lease_owner == "active-worker"


def test_expired_execution_requires_reconciliation(tmp_path, monkeypatch):
    checkpoints = SQLiteCheckpointStore(tmp_path / "runtime.sqlite3")
    _start_run("run-unknown")
    checkpoints.save(
        run_id="run-unknown",
        step_seq=1,
        state="model_retry_executing",
        tool_name="__model__",
        payload=_payload(0),
    )
    monkeypatch.setattr(runtime_control, "get_checkpoint_store", lambda: checkpoints)

    response = asyncio.run(runtime_control.resume_run("run-unknown", _auth()))

    assert response["accepted"] is False
    assert "manual reconciliation" in response["reason"]
    assert response["checkpoint"]["state"] == "reconciliation_required"
    metadata = asyncio.run(get_run_recorder().get_metadata("run-unknown"))
    assert metadata["status"] == RunStatus.FAILED.value
    assert metadata["terminal_reason"] == "model_outcome_unknown"