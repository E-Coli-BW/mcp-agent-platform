"""Tests for the C3 subagent verifier — graded grading of subagent answers.

Three concerns, three test classes:
  TestParseVerifierResponse — tolerant JSON extraction
  TestVerifySubagentAnswer  — the standalone verifier function
                              (flag, empty answer, grade thresholds,
                              fail-open invariants)
  TestSpawnVerifierIntegration — end-to-end through spawn_subagent
                                 with a faked _run_child (markers,
                                 retry, event publish)

Run:
    cd platform/agent-server
    .venv/bin/python -m pytest tests/test_subagent_verifier.py -q
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.agent.subagent_verifier import (
    VerifyVerdict,
    _parse_verifier_response,
    _reset_verifier_cache_for_tests,
    format_retry_brief,
    format_verifier_marker,
    verify_subagent_answer,
)


@pytest.fixture(autouse=True)
def _clear_verifier_cache():
    """Tests inject fake critics; ensure cache doesn't leak between
    tests by pre-populating with stale entries."""
    _reset_verifier_cache_for_tests()
    yield
    _reset_verifier_cache_for_tests()


@pytest.fixture
def _enable_verifier(monkeypatch):
    """Flip the C3 feature flag on. Same monkeypatch-the-live-settings
    pattern as C1 _enable_reflexion fixture — node reads settings each
    invocation."""
    from app.config import settings as live_settings
    monkeypatch.setattr(live_settings, "subagent_verifier_enabled", True)
    monkeypatch.setattr(live_settings, "subagent_verifier_min_grade", 3)
    monkeypatch.setattr(live_settings, "subagent_verifier_auto_retry", True)


# ── 1. JSON parsing tolerance ─────────────────────────────────────────


class TestParseVerifierResponse:
    """Same tolerance set as C1's _parse_critic_response — small models
    botch JSON often, parser must absorb it without raising."""

    def test_should_parseCleanJson(self):
        assert _parse_verifier_response(
            '{"grade": 4, "reasoning": "addresses brief"}'
        ) == (4, "addresses brief")

    def test_should_parseJsonWithFences(self):
        raw = '```json\n{"grade": 2, "reasoning": "missing info"}\n```'
        assert _parse_verifier_response(raw) == (2, "missing info")

    def test_should_parseJsonWithLeadingChatter(self):
        raw = "Sure, here's my grade:\n{\"grade\": 5, \"reasoning\": \"good\"}"
        assert _parse_verifier_response(raw) == (5, "good")

    def test_should_returnNone_when_empty(self):
        assert _parse_verifier_response("") is None
        assert _parse_verifier_response("   ") is None

    def test_should_returnNone_when_noJsonObject(self):
        assert _parse_verifier_response("grade is 4 I think") is None

    def test_should_returnNone_when_invalidJson(self):
        assert _parse_verifier_response("{'grade': 3,}") is None

    def test_should_returnNone_when_gradeOutOfRange(self):
        assert _parse_verifier_response('{"grade": 0, "reasoning": "x"}') is None
        assert _parse_verifier_response('{"grade": 6, "reasoning": "x"}') is None
        assert _parse_verifier_response('{"grade": "four", "reasoning": "x"}') is None

    def test_should_truncateLongReasoning(self):
        long = "x" * 500
        result = _parse_verifier_response(f'{{"grade": 2, "reasoning": "{long}"}}')
        assert result is not None
        assert len(result[1]) == 200


# ── 2. verify_subagent_answer ─────────────────────────────────────────


class TestVerifySubagentAnswer:

    @pytest.mark.asyncio
    async def test_should_skip_when_verifierDisabled(self, monkeypatch):
        """Default off → returns skipped verdict regardless of input.
        Back-compat property: existing subagent flows see zero change."""
        from app.config import settings
        # Force-disable in case local `.env` has SUBAGENT_VERIFIER_ENABLED=true
        # (don't let user's local dev config break the "default off" test).
        monkeypatch.setattr(settings, "subagent_verifier_enabled", False)
        verifier = AsyncMock()
        verdict = await verify_subagent_answer(
            brief="find X",
            answer="X is Y",
            verifier_model=verifier,
        )
        assert verdict.passed is None
        assert verdict.grade is None
        verifier.ainvoke.assert_not_called()

    @pytest.mark.asyncio
    async def test_should_skip_when_emptyAnswer(self, _enable_verifier):
        """Empty answer → already a failure signal upstream; no need to
        burn a verifier call on it. Returns skipped, not False."""
        verifier = AsyncMock()
        verdict = await verify_subagent_answer(
            brief="find X",
            answer="",
            verifier_model=verifier,
        )
        assert verdict.passed is None
        verifier.ainvoke.assert_not_called()

    @pytest.mark.asyncio
    async def test_should_skip_when_emptyBrief(self, _enable_verifier):
        """Defensive — no brief = nothing to grade against."""
        verifier = AsyncMock()
        verdict = await verify_subagent_answer(
            brief="",
            answer="some answer",
            verifier_model=verifier,
        )
        assert verdict.passed is None

    @pytest.mark.asyncio
    async def test_should_pass_when_gradeAboveThreshold(self, _enable_verifier):
        verifier = AsyncMock()
        verifier.ainvoke.return_value = MagicMock(
            content='{"grade": 4, "reasoning": "ok"}'
        )
        verdict = await verify_subagent_answer(
            brief="find X", answer="X is Y", verifier_model=verifier,
        )
        assert verdict.passed is True
        assert verdict.grade == 4
        assert verdict.reasoning == "ok"

    @pytest.mark.asyncio
    async def test_should_fail_when_gradeBelowThreshold(self, _enable_verifier):
        verifier = AsyncMock()
        verifier.ainvoke.return_value = MagicMock(
            content='{"grade": 2, "reasoning": "wrong"}'
        )
        verdict = await verify_subagent_answer(
            brief="find X", answer="X is Y", verifier_model=verifier,
        )
        assert verdict.passed is False
        assert verdict.grade == 2

    @pytest.mark.asyncio
    async def test_should_skip_when_verifierUnparseable(self, _enable_verifier):
        """Verifier returned garbage → fail-open, treat as skipped.
        passed=None (NOT False) so downstream can distinguish 'no signal'
        from 'verifier rejected'."""
        verifier = AsyncMock()
        verifier.ainvoke.return_value = MagicMock(content="I think it's okay")
        verdict = await verify_subagent_answer(
            brief="find X", answer="X is Y", verifier_model=verifier,
        )
        assert verdict.passed is None

    @pytest.mark.asyncio
    async def test_should_skip_when_verifierRaises(self, _enable_verifier):
        """The MOST IMPORTANT invariant: verifier crash MUST NOT fail
        the subagent. Returns skipped verdict, the unverified answer
        ships unchanged."""
        verifier = AsyncMock()
        verifier.ainvoke.side_effect = RuntimeError("LLM down")
        verdict = await verify_subagent_answer(
            brief="find X", answer="X is Y", verifier_model=verifier,
        )
        assert verdict.passed is None
        assert "verifier error" in verdict.reasoning
        assert "RuntimeError" in verdict.reasoning


# ── 3. format_verifier_marker / format_retry_brief ────────────────────


class TestFormatHelpers:

    def test_marker_passthrough_when_passed(self):
        v = VerifyVerdict(passed=True, grade=4, reasoning="ok")
        assert format_verifier_marker(v, "the answer") == "the answer"

    def test_marker_passthrough_when_skipped(self):
        """passed=None (verifier crashed) → ship unmodified. We DON'T
        decorate with ⚠️ here because the verifier never actually
        rejected — it just couldn't grade. Adding a marker would
        falsely imply we know something's wrong."""
        v = VerifyVerdict(passed=None, grade=None, reasoning="crashed")
        assert format_verifier_marker(v, "the answer") == "the answer"

    def test_marker_prepended_when_failed(self):
        v = VerifyVerdict(passed=False, grade=2, reasoning="missed Z")
        marked = format_verifier_marker(v, "the answer")
        assert marked.startswith("⚠️ VERIFIER (grade 2/5): missed Z")
        assert "the answer" in marked
        # The original answer must be preserved verbatim BELOW the
        # marker — parent agents may parse the section.
        assert "---" in marked

    def test_retry_brief_carries_original_and_reasoning(self):
        v = VerifyVerdict(passed=False, grade=2, reasoning="missed Z")
        retry = format_retry_brief("find X", v)
        # Both pieces present so the retried child sees what to fix
        # AND what was originally asked.
        assert "missed Z" in retry
        assert "2/5" in retry
        assert "find X" in retry


# ── 4. End-to-end through spawn_subagent ──────────────────────────────


@pytest.fixture
def _fresh_subagent_context():
    """Install a root subagent context so spawn_subagent doesn't reject
    on missing-context. Borrowed from test_subagent.py's pattern."""
    from app.agent.subagent_context import init_root_context, subagent_context
    token = subagent_context.set(init_root_context(
        root_session_id="tenant1:test-root",
        allowed_tools=["memory_search"],
        token_budget=10000,
    ))
    yield
    subagent_context.reset(token)


def _make_fake_run_child(answer: str, error: str | None = None):
    """Build an AsyncMock-style replacement for _run_child that returns
    a SubagentResult without actually invoking LangGraph."""
    from app.agent.subagent import SubagentResult

    async def fake(*, agent, brief, child_session_id, role, depth,
                   deadline_ms, root_session_id=""):
        return SubagentResult(
            child_session_id=child_session_id,
            role=role,
            answer=answer,
            tool_names=["memory_search"],
            prompt_tokens=10,
            completion_tokens=20,
            depth=depth,
            error=error,
        )
    return fake


class TestSpawnVerifierIntegration:
    """End-to-end: drive spawn_subagent with the verifier ON and a
    fake _run_child + injected verifier model. Verifies the contract
    between the spawn orchestrator and the verifier module — including
    the auto-retry path and the child_verified bus event."""

    @pytest.mark.asyncio
    async def test_should_passThrough_when_verifierDisabled(self, _fresh_subagent_context):
        """Verifier off → SubagentResult.verified stays None, answer
        unchanged. The whole new code path is skipped."""
        from app.agent import subagent as subagent_mod
        with (
            patch.object(subagent_mod, "_run_child", new=_make_fake_run_child("the answer")),
            patch.object(subagent_mod, "_build_child_agent", return_value=MagicMock()),
        ):
            result = await subagent_mod.spawn_subagent(
                role="finder",
                brief="find X",
                allowed_tools=["memory_search"],
            )
        assert result.error is None
        assert result.answer == "the answer"
        assert result.verified is None  # default — verifier never ran
        assert result.verifier_grade is None
        assert result.verifier_retried is False

    @pytest.mark.asyncio
    async def test_should_setVerifiedTrue_when_gradePasses(
        self, _enable_verifier, _fresh_subagent_context, monkeypatch
    ):
        from app.agent import subagent as subagent_mod
        from app.agent import subagent_verifier as v_mod

        # Disable auto-retry path so this test only exercises pass.
        monkeypatch.setattr(
            v_mod.settings, "subagent_verifier_auto_retry", False
        )

        async def fake_verify(*, brief, answer, verifier_model=None):
            return VerifyVerdict(passed=True, grade=5, reasoning="great")

        with (
            patch.object(subagent_mod, "_run_child", new=_make_fake_run_child("the answer")),
            patch.object(subagent_mod, "_build_child_agent", return_value=MagicMock()),
            patch("app.agent.subagent_verifier.verify_subagent_answer", new=fake_verify),
        ):
            result = await subagent_mod.spawn_subagent(
                role="finder", brief="find X", allowed_tools=["memory_search"],
            )

        assert result.verified is True
        assert result.verifier_grade == 5
        assert result.verifier_reasoning == "great"
        assert result.verifier_retried is False
        # Passing grade → answer is NOT marked.
        assert result.answer == "the answer"
        assert "⚠️" not in result.answer

    @pytest.mark.asyncio
    async def test_should_markAnswerAndShip_when_failed_and_retryDisabled(
        self, _enable_verifier, _fresh_subagent_context, monkeypatch
    ):
        """auto_retry=False → on fail, prepend ⚠️ marker and ship the
        original answer. Tenants with tight latency SLOs choose this."""
        from app.agent import subagent as subagent_mod
        from app.agent import subagent_verifier as v_mod
        monkeypatch.setattr(v_mod.settings, "subagent_verifier_auto_retry", False)

        async def fake_verify(*, brief, answer, verifier_model=None):
            return VerifyVerdict(passed=False, grade=2, reasoning="missed Z")

        with (
            patch.object(subagent_mod, "_run_child", new=_make_fake_run_child("the answer")),
            patch.object(subagent_mod, "_build_child_agent", return_value=MagicMock()),
            patch("app.agent.subagent_verifier.verify_subagent_answer", new=fake_verify),
        ):
            result = await subagent_mod.spawn_subagent(
                role="finder", brief="find X", allowed_tools=["memory_search"],
            )

        assert result.verified is False
        assert result.verifier_grade == 2
        assert result.verifier_retried is False
        # Marker prepended; original preserved verbatim.
        assert result.answer.startswith("⚠️ VERIFIER (grade 2/5): missed Z")
        assert "the answer" in result.answer

    @pytest.mark.asyncio
    async def test_should_retryAndShipBetter_when_failed_and_retrySucceeds(
        self, _enable_verifier, _fresh_subagent_context
    ):
        """Happy retry path: first fail → retry → second pass. Final
        answer is the retry's answer, verified=True, retried=True."""
        from app.agent import subagent as subagent_mod
        from app.agent.subagent import SubagentResult

        # _run_child called twice — first returns "first", retry "second".
        call_count = {"n": 0}

        async def fake_run_child(*, agent, brief, child_session_id, role,
                                  depth, deadline_ms, root_session_id=""):
            call_count["n"] += 1
            ans = "first attempt" if call_count["n"] == 1 else "second (better)"
            return SubagentResult(
                child_session_id=child_session_id, role=role, answer=ans,
                tool_names=["memory_search"], prompt_tokens=10,
                completion_tokens=20, depth=depth,
            )

        verdict_count = {"n": 0}

        async def fake_verify(*, brief, answer, verifier_model=None):
            verdict_count["n"] += 1
            if verdict_count["n"] == 1:
                return VerifyVerdict(passed=False, grade=2, reasoning="bad")
            return VerifyVerdict(passed=True, grade=5, reasoning="recovered")

        with (
            patch.object(subagent_mod, "_run_child", new=fake_run_child),
            patch.object(subagent_mod, "_build_child_agent", return_value=MagicMock()),
            patch("app.agent.subagent_verifier.verify_subagent_answer", new=fake_verify),
        ):
            result = await subagent_mod.spawn_subagent(
                role="finder", brief="find X", allowed_tools=["memory_search"],
            )

        assert call_count["n"] == 2  # child ran twice
        assert verdict_count["n"] == 2  # both verifications happened
        assert result.verified is True
        assert result.verifier_retried is True
        assert result.answer == "second (better)"  # retry's answer wins
        assert "⚠️" not in result.answer  # no marker — passed second time
        # Token totals accumulated from both attempts.
        assert result.prompt_tokens == 20  # 10 + 10
        assert result.completion_tokens == 40  # 20 + 20

    @pytest.mark.asyncio
    async def test_should_markRetryAnswer_when_retryFailsAgain(
        self, _enable_verifier, _fresh_subagent_context
    ):
        """Retry happens but second verdict still fails. Ship the
        retry's answer with the SECOND verdict's marker — it's the
        most-recent attempt to follow the verifier's guidance."""
        from app.agent import subagent as subagent_mod
        from app.agent.subagent import SubagentResult

        async def fake_run_child(*, agent, brief, child_session_id, role,
                                  depth, deadline_ms, root_session_id=""):
            return SubagentResult(
                child_session_id=child_session_id, role=role,
                answer="retry attempt", tool_names=[], prompt_tokens=5,
                completion_tokens=5, depth=depth,
            )

        verdict_count = {"n": 0}

        async def fake_verify(*, brief, answer, verifier_model=None):
            verdict_count["n"] += 1
            grade = 2 if verdict_count["n"] == 1 else 1
            reason = "first bad" if verdict_count["n"] == 1 else "still wrong"
            return VerifyVerdict(passed=False, grade=grade, reasoning=reason)

        with (
            patch.object(subagent_mod, "_run_child", new=fake_run_child),
            patch.object(subagent_mod, "_build_child_agent", return_value=MagicMock()),
            patch("app.agent.subagent_verifier.verify_subagent_answer", new=fake_verify),
        ):
            result = await subagent_mod.spawn_subagent(
                role="finder", brief="find X", allowed_tools=["memory_search"],
            )

        assert result.verified is False
        assert result.verifier_retried is True
        # Final answer is retry's answer + SECOND verdict's marker (grade 1)
        assert result.answer.startswith("⚠️ VERIFIER (grade 1/5): still wrong")
        assert "retry attempt" in result.answer

    @pytest.mark.asyncio
    async def test_should_failOpen_when_verifierRaises(
        self, _enable_verifier, _fresh_subagent_context
    ):
        """The fail-open invariant end-to-end. Verifier explodes →
        SubagentResult.verified stays None, answer ships unchanged.
        A broken verifier MUST NOT degrade the subagent."""
        from app.agent import subagent as subagent_mod

        async def fake_verify(*, brief, answer, verifier_model=None):
            return VerifyVerdict.skipped("verifier error: RuntimeError")

        with (
            patch.object(subagent_mod, "_run_child", new=_make_fake_run_child("untouched")),
            patch.object(subagent_mod, "_build_child_agent", return_value=MagicMock()),
            patch("app.agent.subagent_verifier.verify_subagent_answer", new=fake_verify),
        ):
            result = await subagent_mod.spawn_subagent(
                role="finder", brief="find X", allowed_tools=["memory_search"],
            )

        assert result.verified is None
        assert result.answer == "untouched"

    @pytest.mark.asyncio
    async def test_should_skipVerifier_when_runChildFailed(
        self, _enable_verifier, _fresh_subagent_context
    ):
        """Don't grade a child that already crashed/timed-out. The
        error field carries the failure signal; verifying empty
        answer is wasteful and also confusing for the bus event."""
        from app.agent import subagent as subagent_mod
        verifier = AsyncMock()

        with (
            patch.object(subagent_mod, "_run_child",
                          new=_make_fake_run_child("", error="timed out")),
            patch.object(subagent_mod, "_build_child_agent", return_value=MagicMock()),
            patch("app.agent.subagent_verifier.verify_subagent_answer",
                   new=AsyncMock(side_effect=AssertionError("should not be called"))),
        ):
            result = await subagent_mod.spawn_subagent(
                role="finder", brief="find X", allowed_tools=["memory_search"],
            )

        assert result.error == "timed out"
        assert result.verified is None  # verifier never ran


# ── 5. child_verified event publish ───────────────────────────────────


class TestChildVerifiedEvent:
    """The child_verified bus event lets the dashboard render
    verified-rates per role. Pin the event publish here so future
    changes can't silently regress what subscribers receive."""

    @pytest.mark.asyncio
    async def test_should_publishChildVerifiedEvent_when_verifierRan(
        self, _enable_verifier, _fresh_subagent_context, monkeypatch
    ):
        import asyncio

        from app.agent import subagent as subagent_mod
        from app.agent import subagent_verifier as v_mod
        from app.agent import fleet_bus

        # Confirm event type is registered in EVENT_TYPES — this is the
        # contract subscribers rely on.
        assert "child_verified" in fleet_bus.EVENT_TYPES

        # Auto-retry off so we get one verifier run + one event.
        monkeypatch.setattr(v_mod.settings, "subagent_verifier_auto_retry", False)

        async def fake_verify(*, brief, answer, verifier_model=None):
            return VerifyVerdict(passed=True, grade=4, reasoning="solid")

        # MUST match the root_session_id installed by
        # _fresh_subagent_context — that's the key the bus routes
        # events on (publish_event reads it from subagent_context).
        root_id = "tenant1:test-root"
        await fleet_bus.register_session(root_id)

        # Drainer task uses subscribe() async-generator, terminating on
        # child_verified — mirrors the canonical pattern in
        # tests/test_fleet_bus.py to keep the bus contract honoured.
        received: list[dict] = []

        async def reader():
            async for ev in fleet_bus.subscribe(root_id):
                received.append(ev)
                if ev["type"] == "child_verified":
                    return

        task = asyncio.create_task(reader())
        # Give the subscriber a tick to install its queue before we
        # publish anything (same pattern as test_fleet_bus.py L104).
        await asyncio.sleep(0)
        try:
            with (
                patch.object(subagent_mod, "_run_child",
                              new=_make_fake_run_child("the answer")),
                patch.object(subagent_mod, "_build_child_agent",
                              return_value=MagicMock()),
                patch("app.agent.subagent_verifier.verify_subagent_answer",
                       new=fake_verify),
            ):
                await subagent_mod.spawn_subagent(
                    role="finder", brief="find X",
                    allowed_tools=["memory_search"],
                )

            await asyncio.wait_for(task, timeout=2.0)
        finally:
            await fleet_bus.unregister_session(root_id)

        verified_events = [e for e in received if e["type"] == "child_verified"]
        assert len(verified_events) == 1
        ev = verified_events[0]
        assert ev["verified"] is True
        assert ev["grade"] == 4
        assert ev["reasoning"] == "solid"
        assert ev["retried"] is False

    @pytest.mark.asyncio
    async def test_should_notPublishChildVerifiedEvent_when_verifierSkipped(
        self, _enable_verifier, _fresh_subagent_context
    ):
        """Skipped verdict → no event. Subscribers treat the absence
        of child_verified as 'no signal' — consistent with the doc
        on EVENT_TYPES.

        Implementation note: 'no event published' is a negative property,
        so a queue-drainer would race the verifier and produce flakes.
        We instead spy on fleet_bus.publish_event from inside the
        subagent module and assert child_verified is never among the
        recorded publish calls. This makes the test deterministic
        regardless of any future async-ordering changes."""
        from app.agent import subagent as subagent_mod

        async def fake_verify(*, brief, answer, verifier_model=None):
            return VerifyVerdict.skipped("crashed")

        recorded: list[dict] = []

        def fake_publish(**kwargs):
            recorded.append(kwargs)

        with (
            patch.object(subagent_mod, "_run_child",
                          new=_make_fake_run_child("the answer")),
            patch.object(subagent_mod, "_build_child_agent",
                          return_value=MagicMock()),
            patch("app.agent.subagent_verifier.verify_subagent_answer",
                   new=fake_verify),
            # Patch publish_event AT THE SITE WHERE IT'S CALLED FROM
            # (subagent_mod) — not at the source. This is the standard
            # mock.patch lookup-on-bind discipline.
            patch.object(subagent_mod, "publish_event", new=fake_publish),
        ):
            await subagent_mod.spawn_subagent(
                role="finder", brief="find X",
                allowed_tools=["memory_search"],
            )

        # Nothing the subagent module publishes should be child_verified
        # when the verifier produced a skipped verdict (passed=None).
        assert not any(
            call.get("event_type") == "child_verified" for call in recorded
        )


# ── 6. VerifyVerdict invariants ───────────────────────────────────────


class TestVerifyVerdict:

    def test_is_frozen(self):
        """Frozen so a downstream patch can't accidentally mutate the
        passed/grade fields after they're set by the verifier — those
        decisions are load-bearing for the audit log."""
        v = VerifyVerdict(passed=True, grade=4, reasoning="ok")
        with pytest.raises(Exception):
            v.passed = False  # type: ignore[misc]

    def test_skipped_helper(self):
        v = VerifyVerdict.skipped("disabled")
        assert v.passed is None
        assert v.grade is None
        assert v.reasoning == "disabled"
