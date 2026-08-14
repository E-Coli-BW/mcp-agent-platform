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
    AgentTerminationError,
    ContextBudgetExceededError,
    _build_llm_messages,
    _build_llm_view,
    _deduplicate_tool_calls,
    _format_tool_error,
    build_agent_graph,
    inject_context_node,
    make_error_tracking_node,
    make_hallucination_fix_node,
    make_route_node,
    make_synthesize_artifact_node,
    reset_turn_state_node,
    route_after_artifact_synthesis,
    update_investigation_node,
)
from app.artifacts import ArtifactRegistry, VerifiedArtifact

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
    assert "reset_turn_state" in node_names
    assert "synthesize_artifact" in node_names
    assert "update_investigation_initial" in node_names
    assert "update_investigation_tools" in node_names
    assert "compress_history" not in node_names
    assert "context_compressor" not in node_names


def test_deduplicate_tool_calls_drops_exact_duplicates():
    response = AIMessage(
        content="",
        tool_calls=[
            {"id": "first", "name": "file_list", "args": {"path": "infrastructure"}},
            {"id": "duplicate", "name": "file_list", "args": {"path": "infrastructure"}},
            {"id": "other", "name": "file_read", "args": {"path": "infrastructure/template.yml"}},
        ],
    )

    deduplicated = _deduplicate_tool_calls(response)

    assert [call["id"] for call in deduplicated.tool_calls] == ["first", "other"]
    assert len(response.tool_calls) == 3


def test_deduplicate_tool_calls_preserves_different_arguments():
    response = AIMessage(
        content="",
        tool_calls=[
            {"id": "one", "name": "file_read", "args": {"path": "one.yml"}},
            {"id": "two", "name": "file_read", "args": {"path": "two.yml"}},
        ],
    )

    assert _deduplicate_tool_calls(response) is response


def test_format_tool_error_classifies_ordinary_error():
    assert _format_tool_error(RuntimeError("boom")) == (
        "Error [TOOL_ERROR]: RuntimeError: boom"
    )


def test_format_tool_error_classifies_and_bounds_timeout():
    content = _format_tool_error(TimeoutError("x" * 3000))

    assert content.startswith("Error [TOOL_TIMEOUT]: TimeoutError:")
    assert len(content) < 2100
    assert content.endswith("... (truncated)")


def test_reset_turn_state_preserves_checkpointed_context():
    state = AgentState(
        messages=[HumanMessage(content="continue the investigation")],
        workspace_context="workspace",
        investigation=InvestigationState(confirmed_facts=["template.yml exists"]),
        loop_counter=12,
        consecutive_errors=2,
        critique_attempts=1,
        compression_summary="old summary",
    )

    result = reset_turn_state_node(state)

    assert result == {
        "loop_counter": 0,
        "consecutive_errors": 0,
        "critique_attempts": 0,
        "compression_summary": "",
    }
    assert state.messages[0].content == "continue the investigation"
    assert state.workspace_context == "workspace"
    assert state.investigation.confirmed_facts == ["template.yml exists"]


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
    with (
        patch("app.context.workspace.get_workspace_context", return_value="Python project"),
        patch("app.tools.agent_mode.get_workspace_root", return_value="/tmp"),
    ):
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


def test_plugin_artifact_becomes_deterministic_checkpointed_answer():
    class DemoPlugin:
        plugin_id = "demo"

        def extract_file(self, **kwargs):
            return None

        def accepts_request(self, prompt, artifact):
            return "explain" in prompt

        def required_tools(self, prompt, tool_outputs):
            return ()

        def validate(self, artifact):
            return artifact.artifact_type == "summary"

        def render_answer(self, artifact):
            return f"Verified: {artifact.content}"

    artifact = VerifiedArtifact(
        plugin_id="demo",
        artifact_type="summary",
        source_path="evidence.txt",
        content="deterministic evidence",
    )
    state = AgentState(
        messages=[
            HumanMessage(content="explain this evidence"),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "read-1",
                        "name": "file_read",
                        "args": {"path": "evidence.txt"},
                    }
                ],
            ),
            ToolMessage(
                content=artifact.to_tool_block(),
                tool_call_id="read-1",
                name="file_read",
            ),
        ]
    )

    node = make_synthesize_artifact_node(ArtifactRegistry([DemoPlugin()]))
    result = node(state)
    answer = result["messages"][0]

    assert answer.content == "Verified: deterministic evidence"
    assert answer.additional_kwargs["verified_artifact"] == {
        "plugin_id": "demo",
        "artifact_type": "summary",
        "source_path": "evidence.txt",
    }

    routed_state = AgentState(messages=[*state.messages, answer])
    assert route_after_artifact_synthesis(routed_state) == "__end__"


def test_artifact_synthesis_falls_back_when_no_plugin_accepts():
    node = make_synthesize_artifact_node(ArtifactRegistry())
    state = AgentState(messages=[HumanMessage(content="explain this project")])

    assert node(state) == {}
    assert route_after_artifact_synthesis(state) == "call_llm"


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


@pytest.mark.parametrize(
    "content",
    [
        "I will now explore the environment directory.",
        "Let me read the remaining config files.",
        "Next, let's examine the production configuration.",
        "The next step is to search for broader terms.",
    ],
)
def test_route_retries_unfinished_action_after_tool_result(content):
    route = make_route_node(max_steps=20)
    state = AgentState(
        messages=[
            AIMessage(
                content="",
                tool_calls=[{"id": "1", "name": "file_search", "args": {"query": "x"}}],
            ),
            ToolMessage(content="No matches found", tool_call_id="1", name="file_search"),
            AIMessage(content=content),
        ],
        loop_counter=2,
    )

    assert route(state) == "hallucination_fix"


def test_route_requires_primary_source_read_after_search_result():
    route = make_route_node(max_steps=20, available_tools={"file_search", "file_read"})
    state = AgentState(
        messages=[
            HumanMessage(content="read this project and explain the workflow"),
            AIMessage(
                content="",
                tool_calls=[{"id": "1", "name": "file_search", "args": {"query": "workflow"}}],
            ),
            ToolMessage(
                content="src/template.yml:10: StartAt: Validate",
                tool_call_id="1",
                name="file_search",
            ),
            AIMessage(content="The search did not show explicit transitions."),
        ],
        loop_counter=2,
    )

    assert route(state) == "hallucination_fix"


def test_route_allows_completed_tool_summary():
    route = make_route_node(max_steps=20)
    state = AgentState(
        messages=[
            AIMessage(
                content="",
                tool_calls=[{"id": "1", "name": "file_search", "args": {"query": "x"}}],
            ),
            ToolMessage(content="match", tool_call_id="1", name="file_search"),
            AIMessage(content="I searched the project and found the workflow in template.yml."),
        ],
        loop_counter=2,
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
    with pytest.raises(AgentTerminationError) as error:
        route(state)
    assert error.value.terminal_reason == "max_steps_exceeded"


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
    with pytest.raises(AgentTerminationError) as error:
        route(state)
    assert error.value.terminal_reason == "consecutive_tool_errors_exceeded"


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
