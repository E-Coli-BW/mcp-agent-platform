import asyncio

import pytest

from app.runtime.approval import ApprovalRegistry


@pytest.mark.asyncio
async def test_approval_decision_wakes_waiter():
    registry = ApprovalRegistry()
    request = await registry.create(
        run_id="run-1", tenant_id="tenant-a", tool_name="code_shell", reason="approval"
    )
    waiter = asyncio.create_task(registry.wait_for_decision(request.approval_id, timeout=2))
    await asyncio.sleep(0)
    await registry.decide(request.approval_id, status="approved", decided_by="user-1")
    decided = await waiter
    assert decided is not None
    assert decided.status == "approved"
