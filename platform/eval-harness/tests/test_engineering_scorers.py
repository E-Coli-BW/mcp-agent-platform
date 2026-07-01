"""Pure-logic tests for the engineering-effect scorers (efficiency, lift).

These are 100% deterministic — no agent, no LLM. The whole POINT of
these scorers is to test the engineering layer, so their own tests
mustn't depend on a live stack either.
"""
from __future__ import annotations

from eval.case import Case, Expectations, ToolCall, Trajectory
from eval.scorers.efficiency import efficiency
from eval.scorers.lift import lift


def _case(**exp_kwargs) -> Case:
    return Case(id="t", description="", prompt="p", expected=Expectations(**exp_kwargs))


def _traj(
    *,
    answer: str = "the answer is blue",
    tool_calls: int = 2,
    prompt_tokens: int = 500,
    completion_tokens: int = 100,
    baseline_answer: str | None = None,
    baseline_error: str | None = None,
) -> Trajectory:
    return Trajectory(
        case_id="t", prompt="p", final_answer=answer,
        tool_calls=[ToolCall(tool=f"t{i}") for i in range(tool_calls)],
        http_status=200, latency_ms=100,
        prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
        baseline_answer=baseline_answer, baseline_error=baseline_error,
    )


# ── efficiency ───────────────────────────────────────────────────────

class TestEfficiency:
    def test_returns_none_when_no_caps_set(self):
        assert efficiency(_case(), _traj()) is None

    def test_passes_within_caps(self):
        c = _case(max_tool_calls_efficient=3, max_total_tokens_efficient=2000)
        s = efficiency(c, _traj(tool_calls=2, prompt_tokens=500, completion_tokens=100))
        assert s.passed
        assert "tool_calls=2≤3" in s.detail
        assert "tokens=600≤2000" in s.detail

    def test_fails_on_tool_call_bloat(self):
        c = _case(max_tool_calls_efficient=2)
        s = efficiency(c, _traj(tool_calls=5))
        assert not s.passed
        assert "tool_calls=5 > efficient_cap=2" in s.detail

    def test_fails_on_token_bloat_only(self):
        c = _case(max_total_tokens_efficient=500)
        s = efficiency(c, _traj(prompt_tokens=400, completion_tokens=200))
        assert not s.passed
        assert "tokens=600" in s.detail

    def test_token_cap_independent_of_call_cap(self):
        # Only set token cap, lots of calls should be fine
        c = _case(max_total_tokens_efficient=10000)
        s = efficiency(c, _traj(tool_calls=99, prompt_tokens=100, completion_tokens=50))
        assert s.passed                                 # 150 ≤ 10000


# ── lift ─────────────────────────────────────────────────────────────

class TestLift:
    def test_returns_none_when_not_opted_in(self):
        # No `expects_agent_lift` → scorer silent
        c = _case(final_must_contain=["blue"])
        s = lift(c, _traj(baseline_answer="red"))
        assert s is None

    def test_returns_none_when_baseline_not_collected(self):
        # Opted in but runner didn't gather baseline → soft-skip
        c = _case(expects_agent_lift=True, final_must_contain=["blue"])
        s = lift(c, _traj(answer="the answer is blue", baseline_answer=None, baseline_error=None))
        assert s is None

    def test_passes_when_agent_outperforms_baseline(self):
        c = _case(expects_agent_lift=True, final_must_contain=["blue"])
        s = lift(c, _traj(answer="the answer is blue", baseline_answer="I don't know"))
        assert s.passed
        assert "+lift" in s.detail
        assert "agent ✅" in s.detail and "baseline ❌" in s.detail

    def test_passes_but_warns_when_baseline_also_solves_it(self):
        # Both pass → case isn't engineering-discriminative; warn loudly.
        c = _case(expects_agent_lift=True, final_must_contain=["blue"])
        s = lift(c, _traj(
            answer="blue is the color",
            baseline_answer="The color is blue.",
        ))
        assert s.passed                       # don't break the build
        assert "no-lift" in s.detail.lower() or "too easy" in s.detail.lower()

    def test_fails_loudly_when_agent_regresses_vs_baseline(self):
        # The scary one: bare LLM gets it right, our agent doesn't.
        c = _case(expects_agent_lift=True, final_must_contain=["blue"])
        s = lift(c, _traj(
            answer="purple maybe?",
            baseline_answer="The answer is blue.",
        ))
        assert not s.passed
        assert "REGRESSION" in s.detail
        assert "engineering made it WORSE" in s.detail

    def test_fails_when_both_fail(self):
        c = _case(expects_agent_lift=True, final_must_contain=["blue"])
        s = lift(c, _traj(
            answer="I have no idea",
            baseline_answer="dunno",
        ))
        assert not s.passed
        assert "didn't help" in s.detail

    def test_baseline_transport_error_gives_agent_the_win(self):
        # If the bare LLM call literally errored, we can't fairly compare
        # — but if the agent succeeded, that IS lift.
        c = _case(expects_agent_lift=True, final_must_contain=["blue"])
        s = lift(c, _traj(answer="blue", baseline_answer=None, baseline_error="timeout"))
        assert s.passed
        assert "transport-error" in s.detail

    def test_baseline_error_with_agent_fail_is_double_fail(self):
        c = _case(expects_agent_lift=True, final_must_contain=["blue"])
        s = lift(c, _traj(answer="wrong", baseline_answer=None, baseline_error="500"))
        assert not s.passed
        assert "agent ❌" in s.detail
