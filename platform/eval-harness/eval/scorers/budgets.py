"""Scorer: budgets — latency + token budgets.

Returns None when no budget is set on the case (the typical case for
correctness-focused tests). Useful for catching cost regressions:
"this prompt template change quietly doubled prompt tokens".
"""
from __future__ import annotations

from eval.case import Case, Score, Trajectory
from eval.scorers import register


@register
def budgets(case: Case, traj: Trajectory) -> Score | None:
    exp = case.expected
    checks: list[tuple[str, bool, str]] = []

    if exp.max_latency_ms is not None:
        ok = traj.latency_ms <= exp.max_latency_ms
        checks.append(("latency",
                       ok,
                       f"latency {traj.latency_ms}ms vs budget {exp.max_latency_ms}ms"))

    if exp.max_prompt_tokens is not None:
        ok = traj.prompt_tokens <= exp.max_prompt_tokens
        checks.append(("prompt_tokens",
                       ok,
                       f"prompt {traj.prompt_tokens} vs budget {exp.max_prompt_tokens}"))

    if exp.max_completion_tokens is not None:
        ok = traj.completion_tokens <= exp.max_completion_tokens
        checks.append(("completion_tokens",
                       ok,
                       f"completion {traj.completion_tokens} vs budget {exp.max_completion_tokens}"))

    if not checks:
        return None

    passed = all(ok for _, ok, _ in checks)
    detail = "; ".join(d for _, _, d in checks)
    return Score(name="budgets", passed=passed, detail=detail)
