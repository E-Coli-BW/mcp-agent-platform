import pytest

from app.runtime.store import SQLiteRuntimeStore


@pytest.mark.asyncio
async def test_recovery_releases_processing_invocations(tmp_path):
    store = SQLiteRuntimeStore(tmp_path / "runtime.sqlite3")
    await store.start_run(run_id="run-1", request_id="req-1", tenant_id="tenant", session_id="s1", model="test")
    await store.claim(
        idempotency_key="tool-1", args_hash="sha256:x",
        invocation={"run_id": "run-1", "tool_name": "code_shell", "risk": "execute"},
    )
    assert await store.recover_run("run-1") == 1
    record = await store.claim(
        idempotency_key="tool-1", args_hash="sha256:x",
        invocation={"run_id": "run-1", "tool_name": "code_shell", "risk": "execute"},
    )
    assert record.should_execute is True
