import asyncio

import pytest

from app.agent.model.request_recovery import (
    ModelRetryFingerprintError,
    model_retry_lease_owner,
)
from app.runtime import get_run_recorder, reset_run_recorder_for_tests
from app.runtime.checkpoint import SQLiteCheckpointStore
from app.runtime.model_retry_recovery import resume_model_retry_run


@pytest.fixture(autouse=True)
def reset_recorder():
    asyncio.run(reset_run_recorder_for_tests())
    yield
    asyncio.run(reset_run_recorder_for_tests())


@pytest.mark.asyncio
async def test_fingerprint_drift_requires_reconciliation(tmp_path, monkeypatch):
    store = SQLiteCheckpointStore(tmp_path / "runtime.sqlite3")
    store.save(
        run_id="run-drift",
        step_seq=1,
        state="model_retry_executing",
        tool_name="__model__",
        payload={
            "kind": "model_retry",
            "session_id": "tenant-a:session",
            "prompt_version": "v2",
            "attempts_failed": 1,
        },
    )
    checkpoint = store.claim(
        "run-drift",
        owner=model_retry_lease_owner("run-drift"),
        expected_state="model_retry_executing",
    )
    assert checkpoint is not None
    await get_run_recorder().start_run(
        run_id="run-drift",
        request_id="request-1",
        tenant_id="tenant-a",
        session_id="tenant-a:session",
        model="coding-agent",
    )

    class FakeAgentEngine:
        async def pending_nodes(self, _request):
            return ("call_llm",)

        async def resume(self, _request):
            raise ModelRetryFingerprintError("tool schema changed")

    async def acquire(_session_id, **_kwargs):
        return True

    async def release(_session_id):
        return None

    monkeypatch.setattr(
        "app.runtime.model_retry_recovery.get_agent_engine",
        lambda: FakeAgentEngine(),
    )
    monkeypatch.setattr(
        "app.agent.delegation.governance.init_root_context",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr("app.registry.tool_registry.get_registered_tools", dict)
    monkeypatch.setattr("app.middleware.session_lane.wait_for_session_lock", acquire)
    monkeypatch.setattr("app.middleware.session_lane.release_session_lock", release)

    with pytest.raises(ModelRetryFingerprintError):
        await resume_model_retry_run(
            run_id="run-drift",
            checkpoint=checkpoint,
            metadata={"session_id": "tenant-a:session", "model": "coding-agent"},
            tenant_id="tenant-a",
            user_id="operator",
            store=store,
        )

    assert store.get("run-drift").state == "reconciliation_required"
    metadata = await get_run_recorder().get_metadata("run-drift")
    assert metadata["status"] == "failed"
    assert metadata["terminal_reason"] == "model_retry_fingerprint_mismatch"