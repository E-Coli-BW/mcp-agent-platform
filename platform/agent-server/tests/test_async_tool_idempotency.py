import pytest

from app.context.request_context import set_request_context
from app.runtime import reset_idempotency_store_for_tests
from app.tools.agent_mode import run_idempotent_async_mutation


@pytest.mark.asyncio
async def test_async_mutation_replays_result_without_second_execution():
    await reset_idempotency_store_for_tests()
    set_request_context("req-1", "run-1", "tenant:s1")
    calls = 0

    async def operation():
        nonlocal calls
        calls += 1
        return "ok"

    assert await run_idempotent_async_mutation("code_shell", {"command": "echo ok"}, operation) == "ok"
    assert await run_idempotent_async_mutation("code_shell", {"command": "echo ok"}, operation) == "ok"
    assert calls == 1
    set_request_context("", "", "")
