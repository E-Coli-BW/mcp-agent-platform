from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from app.agent.model.request_recovery import (
    EmptyModelResponseError,
    ModelRequestFailedError,
    ModelRetryFingerprintError,
    ModelRetryLeaseLostError,
    ModelRetryPolicy,
    classify_model_error,
    invoke_with_model_retry,
    validate_model_response,
)
from app.context.request_context import set_agent_contract, set_prompt_version, set_request_context
from app.runtime.checkpoint import SQLiteCheckpointStore


class HttpFailure(RuntimeError):
    def __init__(self, status_code: int):
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


@pytest.mark.asyncio
async def test_retries_server_error_then_returns_response():
    model = AsyncMock()
    model.ainvoke.side_effect = [HttpFailure(500), AIMessage(content="recovered")]
    sleeps = []

    response = await invoke_with_model_retry(
        model,
        [HumanMessage(content="hello")],
        policy=ModelRetryPolicy(max_retries=1, initial_delay_ms=10, jitter_ratio=0),
        sleep=lambda delay: _capture_sleep(sleeps, delay),
    )

    assert response.content == "recovered"
    assert model.ainvoke.await_count == 2
    assert sleeps == [0.01]


@pytest.mark.asyncio
async def test_retries_empty_response_then_returns_response():
    model = AsyncMock()
    model.ainvoke.side_effect = [AIMessage(content=""), AIMessage(content="recovered")]

    response = await invoke_with_model_retry(
        model,
        [HumanMessage(content="hello")],
        policy=ModelRetryPolicy(max_retries=1, initial_delay_ms=0),
    )

    assert response.content == "recovered"
    assert model.ainvoke.await_count == 2


@pytest.mark.asyncio
async def test_does_not_retry_auth_failure():
    model = AsyncMock()
    model.ainvoke.side_effect = HttpFailure(401)

    with pytest.raises(ModelRequestFailedError) as failure:
        await invoke_with_model_retry(
            model,
            [HumanMessage(content="hello")],
            policy=ModelRetryPolicy(max_retries=2, initial_delay_ms=0),
        )

    assert model.ainvoke.await_count == 1
    assert failure.value.terminal_reason == "model_request_failed"
    assert failure.value.retryable is False


def test_rejects_empty_response_without_tool_calls():
    with pytest.raises(EmptyModelResponseError):
        validate_model_response(AIMessage(content="  "))


def test_accepts_tool_call_without_text():
    response = AIMessage(
        content="",
        tool_calls=[{"id": "call-1", "name": "read", "args": {}}],
    )
    assert validate_model_response(response) is response


def test_classifies_retryability():
    assert classify_model_error(HttpFailure(500)).code == "SERVER"
    assert classify_model_error(HttpFailure(500)).retryable is True
    assert classify_model_error(HttpFailure(400)).retryable is False
    assert classify_model_error(EmptyModelResponseError("empty")).code == "EMPTY_RESPONSE"


async def _capture_sleep(delays: list[float], delay: float) -> None:
    delays.append(delay)


def test_lease_loss_preserves_run_for_recovery_owner():
    error = ModelRetryLeaseLostError("transferred")

    assert error.preserve_run is True
    assert error.retryable is True
    assert error.terminal_reason == "model_retry_lease_lost"


@pytest.mark.asyncio
async def test_durable_retry_continues_attempt_count_after_restart(tmp_path, monkeypatch):
    store = SQLiteCheckpointStore(tmp_path / "runtime.sqlite3")
    settings = SimpleNamespace(runtime_store_backend="sqlite")
    monkeypatch.setattr("app.config.settings", settings)
    monkeypatch.setattr("app.runtime.checkpoint.get_checkpoint_store", lambda: store)
    set_request_context("request-1", "run-retry", "tenant:session")
    set_agent_contract("coding-agent")
    set_prompt_version("v2")
    messages = [HumanMessage(content="hello")]
    first = AsyncMock()
    first.ainvoke.side_effect = HttpFailure(500)

    async def crash_during_wait(_delay: float) -> None:
        raise SystemExit("simulated crash")

    with pytest.raises(SystemExit):
        await invoke_with_model_retry(
            first,
            messages,
            policy=ModelRetryPolicy(max_retries=2, initial_delay_ms=0),
            sleep=crash_during_wait,
        )

    waiting = store.get("run-retry")
    assert waiting.state == "model_retry_waiting"
    assert waiting.payload["attempts_failed"] == 1

    with __import__("sqlite3").connect(store.database_path) as connection:
        connection.execute(
            "UPDATE agent_run_checkpoints SET lease_expires_at = ? WHERE run_id = ?",
            (__import__("time").time() - 1, "run-retry"),
        )
    assert store.claim(
        "run-retry",
        owner="recovery-worker",
        expected_state="model_retry_waiting",
    ) is not None
    store.transition("run-retry", state="model_retry_executing")
    resumed = AsyncMock()
    resumed.ainvoke.return_value = AIMessage(content="recovered")

    response = await invoke_with_model_retry(
        resumed,
        messages,
        policy=ModelRetryPolicy(max_retries=2, initial_delay_ms=0),
    )

    assert response.content == "recovered"
    assert resumed.ainvoke.await_count == 1
    assert store.get("run-retry").state == "completed"


@pytest.mark.asyncio
async def test_resume_rejects_request_fingerprint_mismatch(tmp_path, monkeypatch):
    store = SQLiteCheckpointStore(tmp_path / "runtime.sqlite3")
    settings = SimpleNamespace(runtime_store_backend="sqlite")
    monkeypatch.setattr("app.config.settings", settings)
    monkeypatch.setattr("app.runtime.checkpoint.get_checkpoint_store", lambda: store)
    set_request_context("request-1", "run-mismatch", "tenant:session")
    set_agent_contract("coding-agent")
    set_prompt_version("v2")
    store.save(
        run_id="run-mismatch",
        step_seq=1,
        state="model_retry_executing",
        tool_name="__model__",
        payload={
            "kind": "model_retry",
            "request_fingerprint": "sha256:different",
            "attempts_failed": 1,
        },
    )
    assert store.claim(
        "run-mismatch",
        owner="recovery-worker",
        expected_state="model_retry_executing",
    ) is not None
    model = AsyncMock()

    with pytest.raises(ModelRetryFingerprintError):
        await invoke_with_model_retry(
            model,
            [HumanMessage(content="hello")],
            policy=ModelRetryPolicy(max_retries=2, initial_delay_ms=0),
            request_identity={"tools": [{"name": "changed"}]},
        )

    model.ainvoke.assert_not_awaited()