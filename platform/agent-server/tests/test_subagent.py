"""Unit tests for the subagent fleet primitive.

These tests exercise the POLICY layer (subagent_context.py) and the
SPAWN orchestrator (subagent.py) WITHOUT actually invoking an LLM. The
goal is to prove the budget envelope is airtight before we let an LLM
loose inside it.

Run with:
    cd platform/agent-server
    .venv/bin/python -m pytest tests/test_subagent.py -q
"""
from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.agent.subagent_context import (
    DEFAULT_BUDGET_TOKENS,
    DEFAULT_DEADLINE_MS,
    MAX_DEPTH_CEILING,
    MAX_FANOUT_CEILING,
    SpawnRejected,
    SubagentContext,
    derive_child_context,
    get_context,
    init_root_context,
    record_consumption,
    record_fanout,
    subagent_context,
)


# ── Fixtures ───────────────────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def _reset_context():
    """Reset the subagent ContextVar between tests.

    Without this, a test that calls init_root_context leaves the
    context populated for the next test, which then sees stale state.
    The autouse=True + token-reset pattern is the canonical fix.
    """
    token = subagent_context.set(None)  # type: ignore[arg-type]
    yield
    subagent_context.reset(token)


@pytest.fixture
def root_ctx_full_tools():
    """A root context that's permissive enough to let most tests run."""
    return init_root_context(
        root_session_id="test-root",
        allowed_tools=["file_read", "rag_search", "memory_search"],
        token_budget=10000,
        deadline_ms=30_000,
    )


# ── subagent_context tests: policy enforcement ─────────────────────────────
class TestSubagentContextPolicy:
    """Each test isolates ONE policy branch in derive_child_context."""

    def test_should_returnFreshDefault_when_contextNotInitialized(self):
        """get_context without init returns a permissive but bounded default,
        rather than crashing — tests should not need to set up context."""
        ctx = get_context()
        assert ctx.depth == 0
        assert ctx.tokens_remaining == DEFAULT_BUDGET_TOKENS
        assert ctx.allowed_tools == frozenset()  # no spawning allowed by default

    def test_should_installRootContext_when_initCalled(self):
        """init_root_context populates the ContextVar with the requested
        budget and tool allowlist."""
        ctx = init_root_context(
            root_session_id="s1",
            allowed_tools=["file_read", "memory_search"],
            token_budget=5000,
            deadline_ms=10_000,
        )
        assert ctx.depth == 0
        assert ctx.root_session_id == "s1"
        assert ctx.parent_session_id == "s1"
        assert ctx.tokens_remaining == 5000
        assert "file_read" in ctx.allowed_tools
        # The deadline is absolute — within ~1s of (now + 10_000ms)
        target = int(time.time() * 1000) + 10_000
        assert abs(ctx.deadline_unix_ms - target) < 1000
        # Also installed on ContextVar
        assert get_context() is ctx

    def test_should_rejectSpawn_when_depthExceedsCeiling(self, root_ctx_full_tools):
        """A parent already at MAX_DEPTH_CEILING cannot spawn further."""
        # Manufacture a parent at the ceiling
        parent = SubagentContext(
            root_session_id="r",
            parent_session_id="p",
            depth=MAX_DEPTH_CEILING,  # at the ceiling already
            allowed_tools=frozenset(["file_read"]),
            tokens_remaining=10_000,
            deadline_unix_ms=int(time.time() * 1000) + 30_000,
        )
        with pytest.raises(SpawnRejected, match="depth limit"):
            derive_child_context(
                parent,
                child_session_id="c",
                requested_tools=["file_read"],
                estimated_tokens=100,
            )

    def test_should_rejectSpawn_when_fanoutExceedsCeiling(self):
        """A parent that has already spawned MAX_FANOUT children at its
        level cannot spawn a (MAX+1)-th, even if depth+budget are fine."""
        parent = SubagentContext(
            root_session_id="r",
            parent_session_id="p",
            depth=0,
            fanout_used=MAX_FANOUT_CEILING,
            allowed_tools=frozenset(["file_read"]),
            tokens_remaining=10_000,
            deadline_unix_ms=int(time.time() * 1000) + 30_000,
        )
        with pytest.raises(SpawnRejected, match="fanout limit"):
            derive_child_context(
                parent,
                child_session_id="c",
                requested_tools=["file_read"],
                estimated_tokens=100,
            )

    def test_should_rejectSpawn_when_tokenBudgetExceeded(self):
        """If the estimate exceeds the parent's remaining tokens, refuse."""
        parent = SubagentContext(
            root_session_id="r",
            parent_session_id="p",
            depth=0,
            allowed_tools=frozenset(["file_read"]),
            tokens_remaining=500,  # tiny
            deadline_unix_ms=int(time.time() * 1000) + 30_000,
        )
        with pytest.raises(SpawnRejected, match="token budget"):
            derive_child_context(
                parent,
                child_session_id="c",
                requested_tools=["file_read"],
                estimated_tokens=1000,  # > 500 remaining
            )

    def test_should_rejectSpawn_when_deadlinePassed(self):
        """If the wallclock has already expired, refuse — even if other
        budgets look fine."""
        parent = SubagentContext(
            root_session_id="r",
            parent_session_id="p",
            depth=0,
            allowed_tools=frozenset(["file_read"]),
            tokens_remaining=10_000,
            deadline_unix_ms=int(time.time() * 1000) - 1000,  # 1s in the past
        )
        with pytest.raises(SpawnRejected, match="wallclock"):
            derive_child_context(
                parent,
                child_session_id="c",
                requested_tools=["file_read"],
                estimated_tokens=100,
            )

    def test_should_rejectSpawn_when_requestedToolNotInParentAllowlist(self):
        """Child cannot ask for tools the parent doesn't have — monotonic
        narrowing. This is the security-critical invariant."""
        parent = SubagentContext(
            root_session_id="r",
            parent_session_id="p",
            depth=0,
            allowed_tools=frozenset(["file_read"]),  # read only
            tokens_remaining=10_000,
            deadline_unix_ms=int(time.time() * 1000) + 30_000,
        )
        with pytest.raises(SpawnRejected, match="not in the parent's allowlist"):
            derive_child_context(
                parent,
                child_session_id="c",
                requested_tools=["file_write"],  # escalation attempt
                estimated_tokens=100,
            )

    def test_should_rejectSpawn_when_parentHasEmptyAllowlist(self):
        """An agent that wasn't opted into fleet mode cannot spawn at all,
        even if it asks for empty tool list. Safe default."""
        # init_root_context with allowed_tools=None / [] means fleet is
        # disabled for this request.
        parent = init_root_context(
            root_session_id="locked-down",
            allowed_tools=[],  # no fleet
            token_budget=10_000,
            deadline_ms=30_000,
        )
        with pytest.raises(SpawnRejected, match="not enabled"):
            derive_child_context(
                parent,
                child_session_id="c",
                requested_tools=[],
                estimated_tokens=100,
            )

    def test_should_narrowChildContext_when_spawnAllowed(self, root_ctx_full_tools):
        """Happy path: child gets depth+1, fresh fanout, debited budget,
        inherited deadline and allowlist."""
        parent = root_ctx_full_tools  # depth=0, 10000 budget, full allowlist
        child = derive_child_context(
            parent,
            child_session_id="c1",
            requested_tools=["file_read"],
            estimated_tokens=1000,
        )
        assert child.depth == parent.depth + 1
        assert child.fanout_used == 0  # fresh
        assert child.tokens_remaining == parent.tokens_remaining - 1000
        assert child.deadline_unix_ms == parent.deadline_unix_ms  # inherited
        # Allowlist propagates as-is — child can request a further subset
        # at ITS next spawn, but we don't narrow lazily here.
        assert child.allowed_tools == parent.allowed_tools

    def test_should_incrementFanout_when_recordFanoutCalled(self, root_ctx_full_tools):
        """record_fanout is the only way fanout_used can grow on a parent —
        must produce a new dataclass (immutability), not mutate."""
        before = root_ctx_full_tools
        after = record_fanout(before)
        assert after.fanout_used == before.fanout_used + 1
        # Original untouched (immutability):
        assert before.fanout_used == 0

    def test_should_decreaseTokens_when_consumptionRecordedAsPositive(
        self, root_ctx_full_tools
    ):
        """record_consumption(positive) debits tokens; negative refunds.

        Floor at 0 — never let tokens_remaining go negative."""
        ctx = root_ctx_full_tools.__class__(
            **{**root_ctx_full_tools.__dict__, "tokens_remaining": 100}
        )
        after_debit = record_consumption(ctx, 30)
        assert after_debit.tokens_remaining == 70

        after_refund = record_consumption(after_debit, -20)  # refund
        assert after_refund.tokens_remaining == 90

        # Overspend protection: never below zero
        after_overspend = record_consumption(after_refund, 9999)
        assert after_overspend.tokens_remaining == 0


# ── subagent.py orchestrator tests: end-to-end without a real LLM ──────────
class TestSpawnOrchestrator:
    """These tests stub out the child agent to verify orchestration glue
    (build → run → settle budget → format result) without needing an LLM."""

    @pytest.mark.asyncio
    async def test_should_returnErrorResult_when_policyRejects(self):
        """A policy rejection (e.g. empty parent allowlist) must surface
        as a SubagentResult with .error set, NOT as a raised exception.

        Reason: the caller is a LangChain tool whose return value is fed
        back to the LLM. A raised exception would propagate as an opaque
        tool error; a structured result lets the LLM read 'depth limit
        exceeded — try smaller scope' and decide what to do."""
        from app.agent.subagent import spawn_subagent

        # No init → permissive default with EMPTY allowlist → spawn rejected
        result = await spawn_subagent(
            role="anything",
            brief="do something",
            allowed_tools=[],
            max_tool_calls=1,
            max_tokens=100,
        )
        assert result.error is not None
        assert "not enabled" in result.error
        assert result.answer == ""
        # Depth+1 reported even on failure — so audit log sees the attempt
        assert result.depth == 1

    @pytest.mark.asyncio
    async def test_should_propagateRoot_when_deepSpawnRejected(self):
        """A depth-MAX parent that tries to spawn gets a rejection result;
        the parent's own state must NOT be corrupted (no phantom fanout)."""
        from app.agent.subagent import spawn_subagent

        # Install a parent context that's already AT the depth ceiling.
        # Use the ContextVar directly (init_root_context always sets depth=0).
        at_ceiling = SubagentContext(
            root_session_id="r",
            parent_session_id="p",
            depth=MAX_DEPTH_CEILING,
            allowed_tools=frozenset(["file_read"]),
            tokens_remaining=10_000,
            deadline_unix_ms=int(time.time() * 1000) + 30_000,
        )
        subagent_context.set(at_ceiling)

        result = await spawn_subagent(
            role="grandchild",
            brief="any",
            allowed_tools=["file_read"],
            max_tool_calls=2,
            max_tokens=500,
        )
        assert result.error and "depth limit" in result.error
        # Fanout MUST NOT have been incremented on the rejected attempt
        # (otherwise we'd lose a fanout slot for nothing).
        post = get_context()
        assert post.fanout_used == 0

    @pytest.mark.asyncio
    async def test_should_runChild_and_settleBudget_when_happyPath(
        self, root_ctx_full_tools, monkeypatch
    ):
        """End-to-end with a fake child agent: orchestrator builds the
        agent, runs it, captures tool trace, settles budget, formats.

        We patch _build_child_agent to return a fake graph whose
        astream_events yields a synthetic event stream — same shape
        as LangGraph emits in production. This proves the orchestrator
        reads the right event fields without needing a real LLM."""
        from app.agent import subagent as subagent_mod

        async def _fake_stream(state, config, version):
            # Mimic langgraph's astream_events output for one tool round
            yield {
                "event": "on_chat_model_start",
                "data": {"input": {"messages": [[_FakeMsg("the brief text")]]}},
            }
            yield {
                "event": "on_tool_start",
                "name": "file_read",
            }
            yield {
                "event": "on_chat_model_stream",
                "data": {"chunk": _FakeChunk("Answer ")},
            }
            yield {
                "event": "on_chat_model_stream",
                "data": {"chunk": _FakeChunk("text.")},
            }

        fake_agent = MagicMock()
        fake_agent.astream_events = _fake_stream
        monkeypatch.setattr(
            subagent_mod, "_build_child_agent", lambda **kw: fake_agent
        )

        before_remaining = get_context().tokens_remaining

        result = await subagent_mod.spawn_subagent(
            role="reader",
            brief="read foo.py and summarize",
            allowed_tools=["file_read"],
            max_tool_calls=4,
            max_tokens=500,  # reservation upfront
        )

        # Result content
        assert result.error is None
        assert result.answer == "Answer text."
        assert result.tool_names == ["file_read"]
        assert result.completion_tokens > 0
        assert result.depth == 1
        # Budget settled: parent should have been debited by ACTUAL usage
        # (not by the 500-token reservation). Since the fake stream is
        # tiny, actual usage << 500, so the parent's remaining should be
        # higher than (before - 500) but lower than (before).
        after_remaining = get_context().tokens_remaining
        actual_used = result.prompt_tokens + result.completion_tokens
        assert after_remaining == before_remaining - actual_used

    @pytest.mark.asyncio
    async def test_should_timeoutChild_when_streamHangs(
        self, root_ctx_full_tools, monkeypatch
    ):
        """If the child's stream takes longer than the remaining wallclock,
        we get back a SubagentResult with .error mentioning the deadline,
        and we DON'T crash the parent."""
        from app.agent import subagent as subagent_mod

        async def _hang_forever(state, config, version):
            # asyncio.sleep instead of `while True` so the task is
            # cooperatively cancellable when wait_for times out.
            await asyncio.sleep(60)
            yield {"event": "on_chat_model_stream",
                   "data": {"chunk": _FakeChunk("unreachable")}}

        fake_agent = MagicMock()
        fake_agent.astream_events = _hang_forever
        monkeypatch.setattr(
            subagent_mod, "_build_child_agent", lambda **kw: fake_agent
        )

        # Shrink remaining wallclock to 1s so the test finishes fast.
        # (asyncio.wait_for in subagent.py floors at 1s, which is fine
        # for a unit test that just wants to verify timeout handling.)
        narrowed = SubagentContext(
            root_session_id="r",
            parent_session_id="p",
            depth=0,
            allowed_tools=frozenset(["file_read"]),
            tokens_remaining=10_000,
            deadline_unix_ms=int(time.time() * 1000) + 1000,  # 1s
        )
        subagent_context.set(narrowed)

        result = await subagent_mod.spawn_subagent(
            role="hanger",
            brief="...",
            allowed_tools=["file_read"],
            max_tool_calls=1,
            max_tokens=100,
        )
        assert result.error is not None
        assert "wallclock" in result.error or "deadline" in result.error

    def test_should_renderHumanReadable_when_resultFormatted(self):
        """SubagentResult.format_for_llm must produce a string that
        starts with ✅/❌ and includes role + (when success) the answer.
        The LLM reads this directly, so format breakage = silent regression."""
        from app.agent.subagent import SubagentResult

        success = SubagentResult(
            child_session_id="c",
            role="reader",
            answer="The two modules share a TenantContext.",
            tool_names=["file_read", "file_read"],
            prompt_tokens=120,
            completion_tokens=30,
            duration_ms=850,
            depth=1,
        )
        rendered = success.format_for_llm()
        assert rendered.startswith("✅")
        assert "[reader]" in rendered
        assert "file_read" in rendered
        assert "850ms" in rendered
        assert "150 tokens" in rendered  # 120 + 30
        assert "The two modules share a TenantContext." in rendered

        failure = SubagentResult(
            child_session_id="c",
            role="thinker",
            answer="",
            error="depth limit exceeded",
            duration_ms=2,
            depth=2,
        )
        rendered = failure.format_for_llm()
        assert rendered.startswith("❌")
        assert "[thinker]" in rendered
        assert "depth limit exceeded" in rendered


# ── Tiny mock objects mimicking LangChain message/chunk shape ──────────────
class _FakeMsg:
    """Mimic a langchain Message with .content for the prompt-accounting
    branch in _run_child."""
    def __init__(self, content: str):
        self.content = content


class _FakeChunk:
    """Mimic a langchain AIMessageChunk with .content for the streaming
    branch in _run_child."""
    def __init__(self, content: str):
        self.content = content


# ── Prompt-clause regression fences (P1 #2) ────────────────────────────────
# These tests pin the SUBAGENT system-prompt clauses that fix the v4-eval
# telephone effect (children paraphrasing CamelCase identifiers).
#
# IMPORTANT — we deliberately do NOT add similar clauses to the PARENT
# system prompt. Experiment in this session: adding "TRUST the subagent" /
# "summarize concisely" hints to SYSTEM_PROMPT_V2 measurably regressed the
# subagent_parallel_file_summary eval pass rate from ~60% to 0% on
# Qwen2.5:7b. The model over-applied the "summarize, don't paste" hint and
# stopped quoting verbatim even when the user prompt explicitly demanded it.
# Telephone-effect mitigation lives only in the CHILD prompt — that's
# where paraphrasing originates and where the rule has tight scope.
#
# If these clauses are deleted or critical keywords are reworded, these
# tests fail loudly so a reviewer notices BEFORE the eval flips back
# to -lift.
class TestSubagentPromptClauses:
    def test_subagent_prompt_includes_verbatim_quoting_rule(self):
        """The subagent system prompt must forbid paraphrasing identifiers."""
        from app.agent.subagent import _build_subagent_system_prompt
        prompt = _build_subagent_system_prompt(role="file-reader", max_tool_calls=4)
        # Section heading exists.
        assert "VERBATIM" in prompt.upper()
        # Concrete example from the v4-eval failure is referenced so a future
        # maintainer sees WHY the clause is there.
        assert "MAX_DEPTH_CEILING" in prompt
        # The "don't invent a plausible replacement" anti-hallucination rule.
        assert "not found" in prompt.lower() or "do not invent" in prompt.lower()

    def test_subagent_prompt_still_caps_tool_calls(self):
        """Regression: don't lose the existing max_tool_calls advertisement
        while editing the prompt."""
        from app.agent.subagent import _build_subagent_system_prompt
        prompt = _build_subagent_system_prompt(role="x", max_tool_calls=7)
        assert "7 tool calls" in prompt

    def test_subagent_prompt_still_forbids_recursive_spawn(self):
        """Regression: the 'no grandchildren' rule must survive prompt edits."""
        from app.agent.subagent import _build_subagent_system_prompt
        prompt = _build_subagent_system_prompt(role="x", max_tool_calls=4)
        assert "DO NOT spawn more subagents" in prompt

    def test_parent_prompts_stay_minimal_no_subagent_section(self):
        """Regression fence: do NOT add a SUBAGENT section to the parent
        prompts (V1 or V2). Past experiments showed that adding 'trust
        the subagent / summarize concisely' hints to V2 measurably
        regressed Qwen2.5:7b's compliance with explicit user-format
        instructions. Keep parent prompts agnostic; the child prompt
        carries the discipline."""
        from app.agent.prompts import SYSTEM_PROMPT_V1, SYSTEM_PROMPT_V2
        for prompt in (SYSTEM_PROMPT_V1, SYSTEM_PROMPT_V2):
            assert "spawn_subagent" not in prompt, (
                "parent prompt has a spawn_subagent section — past evidence "
                "shows this regresses eval pass rate on small models. Keep "
                "the discipline in the child prompt only."
            )
