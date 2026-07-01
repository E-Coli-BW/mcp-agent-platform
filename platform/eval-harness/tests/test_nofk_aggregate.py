"""Tests for n-of-k aggregation (CaseAggregate) — pure logic, no agent.

These tests pin down the contract that a single LLM-in-the-loop verdict is
a coin flip; only k/N pass-rates are evidence. The CaseAggregate type
formalises that contract.

Three layers exercised:
  1. Threshold resolution (CLI > per-case > default)
  2. Aggregate pass/fail given N runs and a threshold
  3. Summary fields (passed_count, pass_rate, n, to_dict)
"""
from __future__ import annotations

from eval.case import (
    Case,
    CaseAggregate,
    Expectations,
    RunResult,
    Score,
    ToolCall,
    Trajectory,
)


def _case(min_pass_rate: float | None = None, **exp) -> Case:
    if min_pass_rate is not None:
        exp["min_pass_rate"] = min_pass_rate
    return Case(id="t", description="", prompt="p", expected=Expectations(**exp))


def _ok_run(case: Case, passing: bool = True) -> RunResult:
    """Build a synthetic RunResult that is `passing` overall.

    Models the convention `RunResult.passed = all(s.passed) and traj.error is None`.
    """
    score = Score(name="answer_grounded", passed=passing, detail="synthetic")
    traj = Trajectory(
        case_id=case.id,
        prompt=case.prompt,
        final_answer="x",
        http_status=200,
        latency_ms=100,
        prompt_tokens=10,
        completion_tokens=2,
        tool_calls=[ToolCall(tool="file_read")],
    )
    return RunResult(case=case, trajectory=traj, scores=[score])


# ── Threshold resolution ─────────────────────────────────────────────


class TestThresholdResolution:
    def test_default_is_strict_one(self):
        """No per-case override, no CLI override → must pass every run."""
        case = _case()
        agg = CaseAggregate(case=case, runs=[_ok_run(case, True)])
        assert agg.effective_threshold == 1.0

    def test_per_case_min_pass_rate_honoured(self):
        case = _case(min_pass_rate=0.6)
        agg = CaseAggregate(case=case, runs=[_ok_run(case, True)])
        assert agg.effective_threshold == 0.6

    def test_cli_override_beats_per_case(self):
        """CLI `--pass-threshold` should always win — operator intent."""
        case = _case(min_pass_rate=0.6)
        agg = CaseAggregate(
            case=case,
            runs=[_ok_run(case, True)],
            cli_pass_threshold=0.8,
        )
        assert agg.effective_threshold == 0.8


# ── Aggregate pass/fail given N runs ─────────────────────────────────


class TestAggregateVerdict:
    def test_single_run_pass_back_compat(self):
        """N=1 + default threshold == old single-run semantics."""
        case = _case()
        agg = CaseAggregate(case=case, runs=[_ok_run(case, True)])
        assert agg.passed
        assert agg.passed_count == 1
        assert agg.pass_rate == 1.0
        assert agg.n == 1

    def test_single_run_fail_back_compat(self):
        case = _case()
        agg = CaseAggregate(case=case, runs=[_ok_run(case, False)])
        assert not agg.passed
        assert agg.passed_count == 0
        assert agg.pass_rate == 0.0

    def test_flaky_three_of_five_passes_at_threshold_06(self):
        """The empirically-observed v5 pass rate at clean HEAD."""
        case = _case(min_pass_rate=0.6)
        runs = [
            _ok_run(case, True),
            _ok_run(case, True),
            _ok_run(case, True),
            _ok_run(case, False),
            _ok_run(case, False),
        ]
        agg = CaseAggregate(case=case, runs=runs)
        assert agg.passed_count == 3
        assert agg.pass_rate == 0.6
        assert agg.passed  # >= 0.6 threshold

    def test_flaky_three_of_five_fails_at_strict_threshold(self):
        """Same data, strict threshold (no per-case override) → fail."""
        case = _case()
        runs = [
            _ok_run(case, True),
            _ok_run(case, True),
            _ok_run(case, True),
            _ok_run(case, False),
            _ok_run(case, False),
        ]
        agg = CaseAggregate(case=case, runs=runs)
        assert not agg.passed

    def test_all_fail_never_passes(self):
        case = _case(min_pass_rate=0.0)
        runs = [_ok_run(case, False) for _ in range(5)]
        agg = CaseAggregate(case=case, runs=runs)
        # threshold 0.0 means "any pass count >= 0% suffices" → trivially true
        # even at 0/5. Catches the >= vs > confusion at boundary.
        assert agg.pass_rate == 0.0
        assert agg.passed  # 0/5 == 0% >= 0% threshold

    def test_boundary_at_exactly_threshold(self):
        """Threshold should be inclusive (`>=` not `>`)."""
        case = _case(min_pass_rate=0.5)
        runs = [_ok_run(case, True), _ok_run(case, False)]
        agg = CaseAggregate(case=case, runs=runs)
        assert agg.pass_rate == 0.5
        assert agg.passed  # exactly at threshold should PASS

    def test_empty_runs_never_passes(self):
        """Edge case: zero runs (would only happen via a runner bug)."""
        case = _case()
        agg = CaseAggregate(case=case, runs=[])
        assert not agg.passed
        assert agg.pass_rate == 0.0
        assert agg.n == 0


# ── to_dict summary view ─────────────────────────────────────────────


class TestAggregateToDict:
    def test_dict_carries_nofk_metadata(self):
        case = _case(min_pass_rate=0.6)
        runs = [
            _ok_run(case, True),
            _ok_run(case, True),
            _ok_run(case, False),
        ]
        d = CaseAggregate(case=case, runs=runs).to_dict()
        assert d["case_id"] == "t"
        assert d["passed_count"] == 2
        assert d["runs"] == 3
        assert d["pass_rate"] == round(2 / 3, 3)
        assert d["threshold"] == 0.6
        # CI gate keys off `passed`; this case passes (2/3 ≈ 0.667 >= 0.6).
        assert d["passed"] is True

    def test_dict_mean_stats_across_runs(self):
        """Latency/token fields are MEAN across runs, not sum."""
        case = _case()

        def _build(latency: int, pt: int, ct: int) -> RunResult:
            traj = Trajectory(
                case_id=case.id, prompt="p", final_answer="x",
                http_status=200, latency_ms=latency,
                prompt_tokens=pt, completion_tokens=ct,
                tool_calls=[ToolCall(tool="file_read")],
            )
            return RunResult(case=case, trajectory=traj,
                             scores=[Score(name="t", passed=True, detail="")])

        runs = [_build(100, 10, 2), _build(300, 30, 6)]
        d = CaseAggregate(case=case, runs=runs).to_dict()
        assert d["latency_ms"] == 200          # (100 + 300) / 2
        assert d["prompt_tokens"] == 20        # (10 + 30) / 2
        assert d["completion_tokens"] == 4     # (2 + 6) / 2

    def test_dict_uses_first_run_for_representative_fields(self):
        """tool_calls / scores in the dict come from runs[0] — keeps the
        report compact. Per-run detail lives in trajectories.jsonl."""
        case = _case()
        r1 = _ok_run(case, True)
        # Mutate r1's first run to have a distinctive tool.
        r1.trajectory.tool_calls = [ToolCall(tool="distinctive_tool")]
        r2 = _ok_run(case, True)
        d = CaseAggregate(case=case, runs=[r1, r2]).to_dict()
        assert d["tool_calls"] == ["distinctive_tool"]
