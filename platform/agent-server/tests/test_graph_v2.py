"""Tests for graph_v2 — explicit StateGraph agent.

Tests the individual nodes and routing logic in isolation,
without requiring an LLM runtime.
"""

from unittest.mock import MagicMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

from app.agent.compressor import InvestigationState, estimate_chars
from app.agent.graph_v2 import (
    AgentState,
    ContextBudgetExceededError,
    _build_llm_messages,
    _build_llm_view,
    build_agent_graph,
    inject_context_node,
    make_error_tracking_node,
    make_hallucination_fix_node,
    make_route_node,
    update_investigation_node,
)


# ── AgentState ────────────────────────────────────────────────


def test_agent_state_defaults():
    state = AgentState()
    assert state.messages == []
    assert state.workspace_context == ""
    assert state.loop_counter == 0
    assert state.consecutive_errors == 0
    assert state.investigation == InvestigationState()
    # Reflexion (C1) — critique_attempts must start at 0 so the cap
    # logic in reflexion.py works on fresh state.
    assert state.critique_attempts == 0


def test_build_agent_graph_uses_model_view_only_compression():
    graph = build_agent_graph(
        model=MagicMock(),
        tools=[],
        system_prompt="system",
    )

    node_names = set(graph.get_graph().nodes)
    assert "update_investigation_initial" in node_names
    assert "update_investigation_tools" in node_names
    assert "compress_history" not in node_names
    assert "context_compressor" not in node_names


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
    long_msgs = [HumanMessage(id=f"message-{index}", content="x" * 1000) for index in range(20)]
    state = AgentState(messages=long_msgs)
    with patch("app.agent.graph_v2.settings") as mock_settings:
        mock_settings.max_context_chars = 10000
        msgs = _build_llm_messages(state, "system")
    assert estimate_chars(msgs) <= 10000


def test_build_llm_view_budgets_system_workspace_and_skills():
    state = AgentState(
        messages=[HumanMessage(content="m" * 1000)],
        workspace_context="w" * 6000,
        skills_catalog="s" * 3000,
    )
    with patch("app.agent.graph_v2.settings") as mock_settings:
        mock_settings.max_context_chars = 5000
        view = _build_llm_view(state, "system")

    assert view.overflow is False
    assert view.total_chars <= 5000
    assert "optional context truncated" in view.compression_summary


def test_build_llm_messages_raises_when_required_floor_exceeds_budget():
    state = AgentState(messages=[HumanMessage(content="x" * 5000)])
    with patch("app.agent.graph_v2.settings") as mock_settings:
        mock_settings.max_context_chars = 1000
        with pytest.raises(ContextBudgetExceededError):
            _build_llm_messages(state, "system")


def test_build_llm_view_injects_structured_investigation_facts():
    messages = [HumanMessage(content="fix the bug")]
    messages.extend(AIMessage(content="middle" * 200) for _ in range(8))
    messages.extend(AIMessage(content="recent" * 15) for _ in range(6))
    state = AgentState(
        messages=messages,
        investigation=InvestigationState(confirmed_facts=["root cause confirmed"]),
    )
    with patch("app.agent.graph_v2.settings") as mock_settings:
        mock_settings.max_context_chars = 1200
        view = _build_llm_view(state, "system")

    assert view.overflow is False
    assert any("root cause confirmed" in str(message.content) for message in view.messages)


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
    with patch("app.agent.graph_v2.settings") as mock_settings:
        mock_settings.max_context_chars = 12000
        msgs = _build_llm_messages(state, "system")
    model_tool_messages = [message for message in msgs if isinstance(message, ToolMessage)]
    assert len(model_tool_messages[0].content) < 3000
    persisted_tool_messages = [
        message for message in state.messages if isinstance(message, ToolMessage)
    ]
    assert len(persisted_tool_messages[0].content) == 3000


def test_update_investigation_node_extracts_confirmed_fact():
    state = AgentState(
        messages=[
            HumanMessage(content="fix the race"),
            AIMessage(content="The root cause is concurrent mutation of the cache."),
        ]
    )

    result = update_investigation_node(state)

    assert result["investigation"].goal == "fix the race"
    assert "concurrent mutation" in result["investigation"].confirmed_facts[0]
    assert state.investigation == InvestigationState()


def _build_investigation_checkpoint_graph(checkpointer):
    graph = StateGraph(AgentState)
    graph.add_node("update_investigation", update_investigation_node)
    graph.add_edge(START, "update_investigation")
    graph.add_edge("update_investigation", END)
    return graph.compile(checkpointer=checkpointer)


def test_checkpoint_round_trip_preserves_messages_and_investigation(tmp_path):
    database = str(tmp_path / "agent-state.sqlite3")
    config = {"configurable": {"thread_id": "compression-contract"}}
    messages = [
        HumanMessage(id="goal", content="fix the race"),
        AIMessage(
            id="finding",
            content="The root cause is concurrent mutation of the cache.",
        ),
    ]

    with SqliteSaver.from_conn_string(database) as first_saver:
        first_saver.setup()
        graph = _build_investigation_checkpoint_graph(first_saver)
        graph.invoke({"messages": messages}, config)

    with SqliteSaver.from_conn_string(database) as second_saver:
        second_saver.setup()
        graph = _build_investigation_checkpoint_graph(second_saver)
        state = graph.get_state(config).values

    assert [message.id for message in state["messages"]] == ["goal", "finding"]
    investigation = InvestigationState.model_validate(state["investigation"])
    assert investigation.goal == "fix the race"
    assert "concurrent mutation" in investigation.confirmed_facts[0]


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


def test_hallucination_fix_receives_available_tools():
    state = AgentState(messages=[HumanMessage(content="remember my name is Hao")])

    result = make_hallucination_fix_node({"memory_set"})(state)

    assert "memory_set" in str(result["messages"][0].content)
