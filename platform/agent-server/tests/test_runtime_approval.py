import pytest

from app.runtime.approval import ApprovalRegistry


@pytest.mark.asyncio
async def test_approval_lifecycle_is_single_decision():
    registry = ApprovalRegistry()
    request = await registry.create(
        run_id="run-1", tenant_id="tenant-a", tool_name="code_shell", reason="needs approval"
    )
    assert request.status == "pending"
    decided = await registry.decide(request.approval_id, status="approved", decided_by="user-1")
    assert decided.status == "approved"
    again = await registry.decide(request.approval_id, status="rejected", decided_by="user-2")
    assert again.status == "approved"
