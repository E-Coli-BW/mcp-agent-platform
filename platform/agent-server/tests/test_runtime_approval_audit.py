import pytest

from app.runtime.approval import ApprovalRegistry


@pytest.mark.asyncio
async def test_persisted_approval_preserves_run_identity(tmp_path):
    database = tmp_path / "runtime.sqlite3"
    first = ApprovalRegistry(str(database))
    request = await first.create(
        run_id="run-restart", tenant_id="tenant-a", tool_name="git_commit", reason="approval"
    )
    second = ApprovalRegistry(str(database))
    loaded = await second.get(request.approval_id)
    assert loaded.run_id == "run-restart"
    assert loaded.tenant_id == "tenant-a"
