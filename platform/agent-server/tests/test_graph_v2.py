"""Tests for graph_v2 — explicit StateGraph agent.

Tests the individual nodes and routing logic in isolation,
without requiring an LLM runtime.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from app.agent.graph_v2 import (
    AgentState,
    _build_llm_messages,
    inject_context_node,
    make_compress_history_node,
    make_error_tracking_node,
    make_route_node,
)


# ── AgentState ────────────────────────────────────────────────


def test_agent_state_defaults():
    state = AgentState()
    assert state.messages == []
    assert state.workspace_context == ""
    assert state.loop_counter == 0
    assert state.consecutive_errors == 0
    # Reflexion (C1) — critique_attempts must start at 0 so the cap
    # logic in reflexion.py works on fresh state.
    assert state.critique_attempts == 0


# ── inject_context_node ──────────────────────────────────────


@pytest.mark.asyncio
async def test_inject_context_skips_if_already_set():
    # Both context fields pre-populated → nothing to do
    state = AgentState(workspace_context="already set", skills_catalog="cached")
    result = await inject_context_node(state)
    assert result == {}


@pytest.mark.asyncio
async def test_inject_context_fetches_workspace():
    state = AgentState(skills_catalog="already")  # short-circuit skills fetch
    with patch("app.context.workspace.get_workspace_context", return_value="Python project"):
        with patch("app.tools.agent_mode.get_workspace_root", return_value="/tmp"):
            result = await inject_context_node(state)
    assert "workspace_context" in result


# ── _build_llm_messages ──────────────────────────────────────


def test_build_llm_messages_prepends_system_prompt():
    state = AgentState(
        messages=[HumanMessage(content="hello")],
        workspace_context="Python project",
    )
    msgs = _build_llm_messages(state, "You are a coding agent")
    assert isinstance(msgs[0], SystemMessage)
    assert "coding agent" in msgs[0].content
    assert "Python project" in msgs[0].content
    assert isinstance(msgs[1], HumanMessage)


def test_build_llm_messages_enforces_char_budget():
    # Create messages that exceed budget
    long_msgs = [HumanMessage(content="x" * 5000) for _ in range(10)]
    state = AgentState(messages=long_msgs)
    with patch("app.agent.graph_v2.settings") as mock_settings:
        mock_settings.max_context_chars = 10000
        msgs = _build_llm_messages(state, "system")
    # Should have dropped oldest messages
    # System prompt + some subset of messages
    total_chars = sum(
        len(m.content) for m in msgs if not isinstance(m, SystemMessage)
    )
    assert total_chars <= 10000


def test_build_llm_messages_compresses_old_tool_messages():
    """Old tool messages (beyond last 4) should be compressed."""
    messages = []
    for i in range(6):
        messages.append(
            AIMessage(
                content="",
                tool_calls=[{"id": f"call_{i}", "name": "file_read", "args": {}}],
            )
        )
        messages.append(
            ToolMessage(
                content="x" * 3000,  # Over 1500 threshold
                tool_call_id=f"call_{i}",
                name="file_read",
                id=f"tool_msg_{i}",
            )
        )

    state = AgentState(messages=messages)
    with patch("app.agent.graph._smart_compress", return_value="compressed") as mock:
        msgs = _build_llm_messages(state, "system")
        # First 2 tool messages should be compressed (indices 0,1 of 6 tool msgs)
        assert mock.call_count >= 2


# ── route ─────────────────────────────────────────────────────


def test_route_to_tools_when_tool_calls():
    route = make_route_node(max_steps=20)
    state = AgentState(
        messages=[
            AIMessage(
                content="",
                tool_calls=[{"id": "1", "name": "file_read", "args": {"path": "x"}}],
            )
        ],
        loop_counter=1,
    )
    assert route(state) == "tools"


def test_route_to_end_when_no_tool_calls():
    route = make_route_node(max_steps=20)
    state = AgentState(
        messages=[AIMessage(content="Here is your answer")],
        loop_counter=1,
    )
    assert route(state) == "__end__"


def test_route_to_call_llm_when_critique_humanMessage():
    """Reflexion (C1): when maybe_critique appends a 🪞 HumanMessage,
    route must send back to call_llm so the actor produces a revised
    answer. The plain 'no tool_calls → END' rule would END otherwise.
    Pinned because regressing this silently disables reflexion — every
    test that uses reflexion_enabled=True would still appear to pass
    but stop iterating after the first answer."""
    route = make_route_node(max_steps=20)
    state = AgentState(
        messages=[
            HumanMessage(content="what is X?"),
            AIMessage(content="X is Y"),
            HumanMessage(content="🪞 SELF-CRITIQUE (grade 2/5): missed Z. Please revise."),
        ],
        loop_counter=1,
        critique_attempts=1,
    )
    assert route(state) == "call_llm"


def test_route_to_end_when_plain_humanMessage_at_tail():
    """Inverse of the above: a HumanMessage WITHOUT the 🪞 marker must
    NOT be auto-responded to. This is the resumed-session case where
    a checkpointer reload puts a fresh user turn at the tail — we
    rely on the normal entry path (chat.py invoking the agent) to
    progress, not the router."""
    route = make_route_node(max_steps=20)
    state = AgentState(
        messages=[
            AIMessage(content="previous answer"),
            HumanMessage(content="follow-up question from user"),
        ],
        loop_counter=1,
    )
    assert route(state) == "__end__"


def test_route_to_end_when_max_steps():
    route = make_route_node(max_steps=5)
    state = AgentState(
        messages=[
            AIMessage(
                content="",
                tool_calls=[{"id": "1", "name": "file_read", "args": {}}],
            )
        ],
        loop_counter=5,
    )
    assert route(state) == "__end__"


def test_route_to_end_when_too_many_errors():
    route = make_route_node(max_steps=20)
    state = AgentState(
        messages=[
            AIMessage(
                content="",
                tool_calls=[{"id": "1", "name": "file_read", "args": {}}],
            )
        ],
        loop_counter=1,
        consecutive_errors=4,
    )
    assert route(state) == "__end__"


# ── track_errors ──────────────────────────────────────────────


def test_track_errors_resets_on_success():
    tracker = make_error_tracking_node()
    state = AgentState(
        messages=[
            AIMessage(
                content="",
                tool_calls=[{"id": "1", "name": "file_read", "args": {}}],
            ),
            ToolMessage(content="✅ File contents...", tool_call_id="1", name="file_read"),
        ],
        consecutive_errors=2,
    )
    result = tracker(state)
    assert result["consecutive_errors"] == 0


def test_track_errors_increments_on_error():
    tracker = make_error_tracking_node()
    state = AgentState(
        messages=[
            AIMessage(
                content="",
                tool_calls=[{"id": "1", "name": "file_read", "args": {}}],
            ),
            ToolMessage(content="❌ File not found", tool_call_id="1", name="file_read"),
        ],
        consecutive_errors=0,
    )
    result = tracker(state)
    assert result["consecutive_errors"] == 1
    assert any(isinstance(m, HumanMessage) for m in result.get("messages", []))


def test_track_errors_detects_status_error():
    tracker = make_error_tracking_node()
    msg = ToolMessage(content="bad", tool_call_id="1", name="file_read")
    msg.status = "error"  # LangGraph native error status
    state = AgentState(
        messages=[
            AIMessage(
                content="",
                tool_calls=[{"id": "1", "name": "x", "args": {}}],
            ),
            msg,
        ],
        consecutive_errors=0,
    )
    result = tracker(state)
    assert result["consecutive_errors"] == 1


# ── compress_history ──────────────────────────────────────────


def test_compress_history_noop_when_few_tools():
    compressor = make_compress_history_node()
    state = AgentState(
        messages=[
            ToolMessage(content="short", tool_call_id="1", name="x", id="t1"),
            ToolMessage(content="short", tool_call_id="2", name="x", id="t2"),
        ]
    )
    result = compressor(state)
    assert result == {}


def test_compress_history_compresses_old_large_tools():
    compressor = make_compress_history_node()
    messages = []
    for i in range(6):
        messages.append(
            ToolMessage(
                content="x" * 3000,
                tool_call_id=f"call_{i}",
                name="file_read",
                id=f"tool_{i}",
            )
        )

    state = AgentState(messages=messages)
    with patch("app.agent.graph._smart_compress", return_value="compressed") as mock:
        result = compressor(state)

    assert "messages" in result
    # Should have compressed the first 2 (6 - 4 = 2 old ones)
    assert len(result["messages"]) == 2
    # Verify IDs are preserved for dedup
    assert result["messages"][0].id == "tool_0"
    assert result["messages"][1].id == "tool_1"
