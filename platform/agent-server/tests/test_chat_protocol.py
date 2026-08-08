"""Regression tests for chat stream and thread continuity contracts."""

import inspect
from unittest.mock import MagicMock, patch

import pytest
from langchain_core.messages import AIMessage

from app.api.chat import (
    ChatMessage,
    ChatRequest,
    _FinalResponseBuffer,
    _make_chunk,
    _stream_agent_response,
    _ToolEventTracker,
    _verified_artifact_answer_from_chain_event,
    chat_completions,
)
from app.auth.middleware import AuthContext
from app.context.request_context import set_request_context


def test_tool_execution_uses_named_sse_only():
    source = inspect.getsource(_stream_agent_response)

    assert "yield _make_tool_event(" in source
    assert "<!-- TOOL:" not in source
    assert "yield _make_chunk(chunk.content" not in source
    assert "yield _make_chunk(full_response, model)" in source


def test_final_response_buffer_discards_rejected_draft():
    response_buffer = _FinalResponseBuffer()
    response_buffer.start_call()
    response_buffer.append("unsupported draft")

    response_buffer.start_call()
    response_buffer.append("evidence-based ")
    response_buffer.append("final answer")

    assert response_buffer.text == "evidence-based final answer"


def test_chain_event_exposes_only_marked_verified_artifact_answer():
    marked = AIMessage(
        content="verified artifact",
        additional_kwargs={
            "verified_artifact": {
                "plugin_id": "demo",
                "artifact_type": "summary",
            }
        },
    )
    event = {
        "event": "on_chain_end",
        "data": {"output": {"messages": [marked]}},
    }

    assert _verified_artifact_answer_from_chain_event(event) == "verified artifact"
    assert (
        _verified_artifact_answer_from_chain_event(
            {
                "event": "on_chain_end",
                "data": {"output": {"messages": [AIMessage(content="draft")]}},
            }
        )
        is None
    )


def test_tool_event_tracker_hides_nested_wrappers():
    tracker = _ToolEventTracker()

    assert tracker.start({"run_id": "outer", "parent_ids": ["graph"]}) == 1
    assert tracker.start({"run_id": "wrapped", "parent_ids": ["graph", "outer"]}) is None
    assert tracker.start({"run_id": "inner", "parent_ids": ["graph", "outer", "wrapped"]}) is None
    assert tracker.end({"run_id": "inner"}) is None
    assert tracker.end({"run_id": "wrapped"}) is None
    assert tracker.end({"run_id": "outer"}) == 1
    assert tracker.count == 1


def test_tool_event_tracker_preserves_parallel_outer_runs():
    tracker = _ToolEventTracker()

    assert tracker.start({"run_id": "first", "parent_ids": ["graph"]}) == 1
    assert tracker.start({"run_id": "second", "parent_ids": ["graph"]}) == 2
    assert tracker.end({"run_id": "first"}) == 1
    assert tracker.end({"run_id": "second"}) == 2


@pytest.mark.asyncio
async def test_follow_up_keeps_client_session_id():
    streamed_session_ids = []

    async def fake_stream(messages, model, session_id, tenant_id, **kwargs):
        streamed_session_ids.append(session_id)
        yield _make_chunk("ok", model)
        yield "[DONE]"

    auth = AuthContext(user_id="user", tenant_id="tenant", permissions=[])
    turns = [
        "Find the Step Functions template",
        "ok I tell you more specifically its in infrastructure folder",
    ]

    with (
        patch("app.api.chat._stream_agent_response", new=fake_stream),
        patch("app.agent.subagent_context.init_root_context"),
        patch("app.registry.tool_registry.get_registered_tools", return_value={}),
    ):
        for content in turns:
            response = await chat_completions(
                raw_request=MagicMock(),
                request=ChatRequest(
                    messages=[ChatMessage(role="user", content=content)],
                    stream=False,
                    session_id="conversation-1",
                ),
                auth=auth,
            )
            assert response["choices"][0]["message"]["content"] == "ok"

    assert streamed_session_ids == ["tenant:conversation-1", "tenant:conversation-1"]


@pytest.mark.asyncio
async def test_session_lane_timeout_finishes_run(monkeypatch):
    from app.runtime import chat_stream
    from app.runtime.cancellation import RunCancellationRegistry
    from app.runtime.recorder import InMemoryRunRecorder, RunStatus

    class FakePromptResolution:
        version = "v2"
        content_hash = "sha256:test"

    class FakeConversationStore:
        async def append(self, session_id, message):
            return None

    async def fake_get_agent(*args, **kwargs):
        return MagicMock()

    async def lock_is_held(session_id):
        return True

    async def lock_times_out(session_id, **kwargs):
        return False

    recorder = InMemoryRunRecorder()
    cancellation = RunCancellationRegistry()
    set_request_context("request-lane-timeout", "run-lane-timeout", "tenant:session")
    monkeypatch.setattr(
        chat_stream,
        "resolve_system_prompt",
        lambda **kwargs: FakePromptResolution(),
    )
    monkeypatch.setattr(chat_stream, "get_agent", fake_get_agent)
    monkeypatch.setattr(
        chat_stream,
        "get_conversation_store",
        lambda: FakeConversationStore(),
    )
    monkeypatch.setattr(chat_stream, "get_run_recorder", lambda: recorder)
    monkeypatch.setattr(
        chat_stream,
        "get_run_cancellation_registry",
        lambda: cancellation,
    )
    monkeypatch.setattr(
        "app.middleware.session_lane.session_lock_held",
        lock_is_held,
    )
    monkeypatch.setattr(
        "app.middleware.session_lane.wait_for_session_lock",
        lock_times_out,
    )

    events = [
        event
        async for event in chat_stream.stream_agent_response(
            [ChatMessage(role="user", content="queued request")],
            "coding-agent",
            "tenant:session",
            "tenant",
        )
    ]

    metadata = await recorder.get_metadata("run-lane-timeout")
    assert metadata is not None
    assert metadata["status"] == RunStatus.FAILED.value
    assert metadata["terminal_reason"] == "session_lane_timeout"
    assert metadata["retryable"] is True
    assert events[-1] == "[DONE]"
