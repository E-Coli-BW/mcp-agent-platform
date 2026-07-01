"""Tests for context compression logic."""

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.agent.compressor import (
    InvestigationState,
    compress_messages,
    estimate_tokens,
    make_context_compressor_node,
    update_investigation_from_messages,
)


class TestEstimateTokens:
    def test_string_estimation(self):
        # 4 chars ≈ 1 token
        assert estimate_tokens("a" * 400) == 100

    def test_message_list_estimation(self):
        msgs = [HumanMessage(content="hello world")]  # 11 chars → 2 tokens
        assert estimate_tokens(msgs) == 2


class TestCompressMessages:
    def test_noop_when_under_budget(self):
        msgs = [HumanMessage(content="hello")]
        result, summary = compress_messages(msgs, budget_tokens=1000)
        assert result == msgs
        assert summary == ""

    def test_compresses_large_tool_outputs(self):
        msgs = [
            HumanMessage(content="fix the bug"),
            AIMessage(content="", tool_calls=[{"id": "1", "name": "read_file", "args": {}}]),
            ToolMessage(content="x" * 2000, tool_call_id="1", name="read_file", id="t1"),
            AIMessage(content="", tool_calls=[{"id": "2", "name": "read_file", "args": {}}]),
            ToolMessage(content="y" * 2000, tool_call_id="2", name="read_file", id="t2"),
            AIMessage(content="", tool_calls=[{"id": "3", "name": "read_file", "args": {}}]),
            ToolMessage(content="z" * 100, tool_call_id="3", name="read_file", id="t3"),
            # Last 6 messages (tail) — these 3 + 3 more
            AIMessage(content="I found the issue"),
            HumanMessage(content="great, fix it"),
            AIMessage(content="", tool_calls=[{"id": "4", "name": "edit", "args": {}}]),
            ToolMessage(content="done", tool_call_id="4", name="edit", id="t4"),
            AIMessage(content="Fixed!"),
        ]
        # Budget that forces compression of middle
        result, summary = compress_messages(msgs, budget_tokens=500)
        # Should be shorter than original
        assert estimate_tokens(result) < estimate_tokens(msgs)

    def test_skill_activation_compressed_to_reference(self):
        """When a skill activation message is in the middle zone, it gets compressed to a ref."""
        msgs = [
            HumanMessage(content="build it"),
            AIMessage(content="", tool_calls=[{"id": "1", "name": "shell", "args": {}}]),
            ToolMessage(
                content="[SKILL ACTIVATED: maven-stale-jar-fix]\nYou hit a known error.\n1. Run mvn install...\n" + "x" * 2000,
                tool_call_id="1", name="shell", id="t1",
            ),
            HumanMessage(content="continue " + "y" * 500),
            AIMessage(content="working on it " + "z" * 500, tool_calls=[{"id": "2", "name": "shell", "args": {}}]),
            ToolMessage(content="output " + "w" * 1000, tool_call_id="2", name="shell", id="t2"),
            AIMessage(content="step 2 done " + "a" * 500),
            # Tail (6 messages)
            HumanMessage(content="next"),
            AIMessage(content="", tool_calls=[{"id": "3", "name": "shell", "args": {}}]),
            ToolMessage(content="result3", tool_call_id="3", name="shell", id="t3"),
            AIMessage(content="almost"),
            HumanMessage(content="finish"),
            AIMessage(content="done"),
        ]
        result, _ = compress_messages(msgs, budget_tokens=500)
        # The skill activation in middle should be compressed to a reference
        tool_msgs = [m for m in result if isinstance(m, ToolMessage) and "skill" in (m.content or "").lower()]
        assert len(tool_msgs) > 0
        assert "Applied skill:maven-stale-jar-fix" in tool_msgs[0].content

    def test_failed_tools_compressed_to_first_line(self):
        msgs = [
            HumanMessage(content="do it"),
            AIMessage(content="", tool_calls=[{"id": "1", "name": "shell", "args": {}}]),
            ToolMessage(
                content="❌ Command failed\nLong stack trace\n" + "x" * 1000,
                tool_call_id="1", name="shell", id="t1",
            ),
            # Tail (6 messages)
            HumanMessage(content="a"),
            AIMessage(content="b"),
            HumanMessage(content="c"),
            AIMessage(content="d"),
            HumanMessage(content="e"),
            AIMessage(content="f"),
        ]
        result, summary = compress_messages(msgs, budget_tokens=200)
        tool_msgs = [m for m in result if isinstance(m, ToolMessage)]
        if tool_msgs:
            # Should be truncated to first line
            assert len(tool_msgs[0].content) < 200

    def test_keeps_first_human_message(self):
        """The original user goal must survive compression."""
        msgs = [HumanMessage(content="Fix the NPE in UserService.java")]
        for i in range(20):
            msgs.append(AIMessage(content=f"thinking {i}" * 50, tool_calls=[{"id": str(i), "name": "t", "args": {}}]))
            msgs.append(ToolMessage(content=f"result {i}" * 100, tool_call_id=str(i), name="t", id=f"t{i}"))

        result, _ = compress_messages(msgs, budget_tokens=500)
        # First message preserved
        assert isinstance(result[0], HumanMessage)
        assert "Fix the NPE" in result[0].content


class TestInvestigationState:
    def test_to_summary_block(self):
        state = InvestigationState(
            goal="Fix NPE in UserService",
            confirmed_facts=["Issue is in async callback"],
            current_hypothesis="Race condition on line 142",
            eliminated=["Not a null config — config is loaded correctly"],
            skills_used=["spring-debug-port"],
        )
        block = state.to_summary_block()
        assert "Fix NPE" in block
        assert "async callback" in block
        assert "Race condition" in block
        assert "spring-debug-port" in block

    def test_empty_state_returns_empty(self):
        state = InvestigationState()
        assert state.to_summary_block() == ""

    def test_token_estimate(self):
        state = InvestigationState(goal="a" * 400)
        assert state.token_estimate() > 0


class TestUpdateInvestigation:
    def test_extracts_goal_from_first_human(self):
        state = InvestigationState()
        msgs = [HumanMessage(content="Fix the login bug on production")]
        state = update_investigation_from_messages(state, msgs)
        assert "login bug" in state.goal

    def test_tracks_skill_usage(self):
        state = InvestigationState()
        msgs = [
            ToolMessage(
                content="[SKILL ACTIVATED: maven-fix]\nSteps...",
                tool_call_id="1", name="shell",
            )
        ]
        state = update_investigation_from_messages(state, msgs)
        assert "maven-fix" in state.skills_used

    def test_tracks_eliminations(self):
        state = InvestigationState()
        msgs = [
            ToolMessage(
                content="❌ Connection refused on port 5432",
                tool_call_id="1", name="db_query", status="error",
            )
        ]
        state = update_investigation_from_messages(state, msgs)
        assert len(state.eliminated) == 1
        assert "db_query" in state.eliminated[0]

    def test_extracts_confirmed_facts(self):
        state = InvestigationState()
        msgs = [
            AIMessage(content="The root cause is a race condition in the event loop handler.")
        ]
        state = update_investigation_from_messages(state, msgs)
        assert len(state.confirmed_facts) == 1
        assert "race condition" in state.confirmed_facts[0]


class TestContextCompressorNode:
    def test_noop_when_under_threshold(self):
        """Should return empty dict when under budget."""

        class FakeState:
            messages = [HumanMessage(content="hi")]

        node = make_context_compressor_node(budget_tokens=1000, threshold=0.75)
        result = node(FakeState())
        assert result == {}
