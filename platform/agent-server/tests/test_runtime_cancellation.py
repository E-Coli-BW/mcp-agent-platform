import pytest

from app.runtime.cancellation import RunCancellationRegistry


@pytest.mark.asyncio
async def test_cancellation_registry_requests_active_run_only():
    registry = RunCancellationRegistry()

    assert await registry.request("run-missing") is False
    await registry.register("run-1")
    assert await registry.is_requested("run-1") is False
    assert await registry.request("run-1") is True
    assert await registry.is_requested("run-1") is True
    assert await registry.wait("run-1") is True

    await registry.unregister("run-1")
    assert await registry.request("run-1") is False
    assert await registry.wait("run-1") is False
