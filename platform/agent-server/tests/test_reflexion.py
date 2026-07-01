"""Tests for the C1 reflexion / self-critique node.

These tests cover the critic in isolation — graph integration is
exercised through test_graph_v2.py's route tests. We stub out the
critic model so we can assert on the patch contract (the dict the
node returns) without running a real LLM.

Run:
    cd platform/agent-server
    .venv/bin/python -m pytest tests/test_reflexion.py -q
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from app.agent.graph_v2 import AgentState
from app.agent.reflexion import (
    _find_last_assistant_answer,
    _find_last_user_question,
    _parse_critic_response,
    make_maybe_critique_node,
)


# ── _parse_critic_response: tolerant JSON extraction ───────────────────────
class TestParseCriticResponse:
    """Small models botch JSON ~10-15% of the time. The parser must
    tolerate fenced output, surrounding chatter, and gracefully fail
    (return None) on anything genuinely unparseable — never raise."""

    def test_should_parseCleanJson(self):
        result = _parse_critic_response('{"grade": 4, "reasoning": "looks good"}')
        assert result == (4, "looks good")

    def test_should_parseJsonWithFences(self):
        """Markdown fences are a common small-model habit even when the
        prompt says 'no markdown'. Strip them."""
        raw = '```json\n{"grade": 2, "reasoning": "wrong"}\n```'
        result = _parse_critic_response(raw)
        assert result == (2, "wrong")

    def test_should_parseJsonWithLeadingChatter(self):
        """'Sure, here is my grade:\\n{...}' — the regex finds the first
        {...} block and ignores the prose."""
        raw = "Sure, here is my grade:\n{\"grade\": 5, \"reasoning\": \"perfect\"}"
        result = _parse_critic_response(raw)
        assert result == (5, "perfect")

    def test_should_returnNone_when_emptyInput(self):
        assert _parse_critic_response("") is None
        assert _parse_critic_response("   ") is None

    def test_should_returnNone_when_noJsonObject(self):
        """Free-form text without any {...} = unparseable, fail-open."""
        assert _parse_critic_response("the grade is 4 I think") is None

    def test_should_returnNone_when_invalidJson(self):
        """Malformed brace block (e.g. trailing comma, single quotes) =
        unparseable. Critic failure must never crash the agent."""
        assert _parse_critic_response("{'grade': 3,}") is None

    def test_should_returnNone_when_gradeOutOfRange(self):
        """Grade must be int 1-5. 0, 6, 'four' all rejected."""
        assert _parse_critic_response('{"grade": 0, "reasoning": "x"}') is None
        assert _parse_critic_response('{"grade": 6, "reasoning": "x"}') is None
        assert _parse_critic_response('{"grade": "four", "reasoning": "x"}') is None

    def test_should_truncateLongReasoning(self):
        """Reasoning gets clipped to 200 chars — keeps the revision
        hint terse so it doesn't blow the actor's context window."""
        long_reasoning = "x" * 500
        result = _parse_critic_response(
            f'{{"grade": 2, "reasoning": "{long_reasoning}"}}'
        )
        assert result is not None
        assert len(result[1]) == 200


# ── Message-walking helpers ────────────────────────────────────────────────
class TestMessageHelpers:

    def test_should_findLastUserQuestion_skippingCritique(self):
        """The critic must grade against the ORIGINAL user question,
        not its own earlier critique. Without this guard the critic
        re-judges the revision against its own critique text —
        circular and useless."""
        messages = [
            HumanMessage(content="what is X?"),
            AIMessage(content="X is Y"),
            HumanMessage(content="🪞 SELF-CRITIQUE (grade 2/5): missed Z."),
            AIMessage(content="X is Y, including Z"),
        ]
        assert _find_last_user_question(messages) == "what is X?"

    def test_should_findLastAssistantAnswer_notToolCall(self):
        """AIMessages with tool calls are intermediate reasoning, not
        the final answer the user sees. The critic only grades the
        final answer turn."""
        messages = [
            HumanMessage(content="q"),
            AIMessage(content="", tool_calls=[{"id": "1", "name": "f", "args": {}}]),
            ToolMessage(content="tool result", tool_call_id="1", name="f"),
            AIMessage(content="The answer is 42"),
        ]
        answer = _find_last_assistant_answer(messages)
        assert answer is not None
        assert answer.content == "The answer is 42"

    def test_should_returnNone_when_noFinalAnswerYet(self):
        """Mid-tool-use state: last AIMessage has tool_calls, so there's
        no final answer to critique. Node must no-op."""
        messages = [
            HumanMessage(content="q"),
            AIMessage(content="", tool_calls=[{"id": "1", "name": "f", "args": {}}]),
        ]
        assert _find_last_assistant_answer(messages) is None


# ── Critique node behavior ─────────────────────────────────────────────────
@pytest.fixture
def _enable_reflexion(monkeypatch):
    """Flip reflexion on for the duration of the test. We mutate
    settings instead of dependency_overrides because the node reads
    the live settings on every call."""
    from app.config import settings as live_settings
    monkeypatch.setattr(live_settings, "reflexion_enabled", True)
    monkeypatch.setattr(live_settings, "reflexion_max_attempts", 2)
    monkeypatch.setattr(live_settings, "reflexion_min_grade", 3)


def _state_with_answer(answer_text: str, attempts: int = 0) -> AgentState:
    return AgentState(
        messages=[
            HumanMessage(content="what is the answer?"),
            AIMessage(content=answer_text),
        ],
        critique_attempts=attempts,
    )


class TestMaybeCritiqueNode:

    @pytest.mark.asyncio
    async def test_should_noop_when_reflexionDisabled(self, monkeypatch):
        """Default off → node returns {} regardless of state. This is
        the back-compat property — adding reflexion to the graph must
        not affect any deployment that hasn't opted in."""
        from app.config import settings
        # Force-disable in case local `.env` has REFLEXION_ENABLED=true
        # (don't let user's local dev config break the "default off" test).
        monkeypatch.setattr(settings, "reflexion_enabled", False)
        critic = AsyncMock()
        node = make_maybe_critique_node(critic_model=critic)
        state = _state_with_answer("any answer")
        # reflexion_enabled is False by default, so no fixture here
        result = await node(state)
        assert result == {}
        # Critic must NOT have been called.
        critic.ainvoke.assert_not_called()

    @pytest.mark.asyncio
    async def test_should_noop_when_atAttemptCap(self, _enable_reflexion):
        """Once critique_attempts reaches the cap, future calls pass
        through. Without this we'd loop forever on a grumpy critic."""
        critic = AsyncMock()
        node = make_maybe_critique_node(critic_model=critic)
        state = _state_with_answer("x", attempts=2)  # at cap
        result = await node(state)
        assert result == {}
        critic.ainvoke.assert_not_called()

    @pytest.mark.asyncio
    async def test_should_noop_when_lastMessageIsToolCall(self, _enable_reflexion):
        """Mid-tool-use state has no final answer to grade. Critique
        must wait for the actor to produce a no-tool-call AIMessage."""
        critic = AsyncMock()
        node = make_maybe_critique_node(critic_model=critic)
        state = AgentState(
            messages=[
                HumanMessage(content="q"),
                AIMessage(content="", tool_calls=[{"id": "1", "name": "f", "args": {}}]),
            ],
        )
        result = await node(state)
        assert result == {}
        critic.ainvoke.assert_not_called()

    @pytest.mark.asyncio
    async def test_should_passthrough_when_gradeAboveThreshold(self, _enable_reflexion):
        """grade >= min_grade → silently accept the answer. Node returns
        {} (no state mutation). Logs the grade for observability."""
        critic = AsyncMock()
        critic.ainvoke.return_value = MagicMock(
            content='{"grade": 4, "reasoning": "solid"}'
        )
        node = make_maybe_critique_node(critic_model=critic)
        state = _state_with_answer("a good answer")
        result = await node(state)
        assert result == {}
        # Critic WAS called once.
        assert critic.ainvoke.call_count == 1

    @pytest.mark.asyncio
    async def test_should_injectRevision_when_gradeBelowThreshold(self, _enable_reflexion):
        """grade < min_grade → return {messages: [🪞 HumanMessage], critique_attempts: N+1}.
        This is the core of the reflexion contract."""
        critic = AsyncMock()
        critic.ainvoke.return_value = MagicMock(
            content='{"grade": 2, "reasoning": "missed the main point"}'
        )
        node = make_maybe_critique_node(critic_model=critic)
        state = _state_with_answer("incomplete answer", attempts=0)
        result = await node(state)

        # Contract: messages contains a single 🪞 HumanMessage.
        assert "messages" in result
        assert len(result["messages"]) == 1
        msg = result["messages"][0]
        assert isinstance(msg, HumanMessage)
        content = msg.content if isinstance(msg.content, str) else str(msg.content)
        assert content.startswith("🪞")
        # The grade and reasoning are visible in the hint so the actor
        # sees WHY it needs to revise.
        assert "2/5" in content
        assert "missed the main point" in content

        # Attempt counter bumped.
        assert result["critique_attempts"] == 1

    @pytest.mark.asyncio
    async def test_should_passthrough_when_criticOutputUnparseable(self, _enable_reflexion):
        """Critic failures are fail-open — the unimproved answer is still
        better than no answer. A botched critique must NEVER make the
        agent worse. This is the most-important invariant."""
        critic = AsyncMock()
        critic.ainvoke.return_value = MagicMock(content="I think it is okay maybe?")
        node = make_maybe_critique_node(critic_model=critic)
        state = _state_with_answer("any answer")
        result = await node(state)
        assert result == {}

    @pytest.mark.asyncio
    async def test_should_passthrough_when_criticRaises(self, _enable_reflexion):
        """Same invariant: even if the critic LLM throws (network error,
        OOM, whatever), the agent's answer must flow through unchanged."""
        critic = AsyncMock()
        critic.ainvoke.side_effect = RuntimeError("LLM service down")
        node = make_maybe_critique_node(critic_model=critic)
        state = _state_with_answer("any answer")
        result = await node(state)
        assert result == {}

    @pytest.mark.asyncio
    async def test_should_skipCritiqueMarkerWhenFindingUserQuestion(self, _enable_reflexion):
        """End-to-end verification of the 'critic grades original Q' rule.
        On the SECOND revision pass the message list looks like:
            User Q -> AI A1 -> 🪞 HumanMessage -> AI A2
        The critic must grade A2 against User Q, NOT against the 🪞 marker.
        We assert by inspecting the HumanMessage payload passed to the critic."""
        critic = AsyncMock()
        critic.ainvoke.return_value = MagicMock(
            content='{"grade": 5, "reasoning": "ok"}'
        )
        node = make_maybe_critique_node(critic_model=critic)
        state = AgentState(
            messages=[
                HumanMessage(content="ORIGINAL_QUESTION"),
                AIMessage(content="first draft"),
                HumanMessage(content="🪞 SELF-CRITIQUE (grade 2/5): missing X."),
                AIMessage(content="revised draft"),
            ],
            critique_attempts=1,
        )
        await node(state)
        # The critic was invoked with a 2-message list (system + user).
        call_args = critic.ainvoke.call_args[0][0]
        # The 2nd message (HumanMessage) carries the question + draft.
        critic_user_msg = call_args[1]
        critic_content = critic_user_msg.content if isinstance(
            critic_user_msg.content, str
        ) else str(critic_user_msg.content)
        assert "ORIGINAL_QUESTION" in critic_content
        assert "revised draft" in critic_content
        # Crucially, the critic's own earlier critique must NOT appear
        # as the "question" — that would be circular.
        # (The 🪞 string may appear inside the conversation if we ever
        # included it, but we don't, so we check it's not in the question
        # section.)
        assert "🪞" not in critic_content
