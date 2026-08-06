import pytest

from app.runtime.approval import ApprovalRegistry


@pytest.mark.asyncio
async def test_sqlite_approval_survives_fresh_registry(tmp_path):
    database = tmp_path / "runtime.sqlite3"
    first = ApprovalRegistry(str(database))
    request = await first.create(
        run_id="run-1", tenant_id="tenant-a", tool_name="code_shell", reason="approval"
    )
    second = ApprovalRegistry(str(database))
    loaded = await second.get(request.approval_id)
    assert loaded is not None
    assert loaded.status == "pending"
    decided = await second.decide(request.approval_id, status="rejected", decided_by="user-1")
    assert decided.status == "rejected"
