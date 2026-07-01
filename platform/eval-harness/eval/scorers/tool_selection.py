"""Scorer: tool_selection — did the agent call the right tools?

This is the highest-signal scorer for agent quality. A correct final
answer reached via the WRONG tool path (e.g. memory_search → grep
fallback when memory_set was never called) is still a regression, and
trace-level scoring catches that where prose-matching wouldn't.

Three independent checks, all opt-in:

  expected.tools_called      — required set; every tool must appear at
                                least once (order-independent, multiset OK)
  expected.tools_called_min  — generous floor (catches "agent gave up
                                without using any tool")
  expected.tools_called_max  — ceiling (catches runaway loops)
  expected.tools_forbidden   — must NOT call (security/policy guard)
"""
from __future__ import annotations

from collections import Counter

from eval.case import Case, Score, Trajectory
from eval.scorers import register


@register
def tool_selection(case: Case, traj: Trajectory) -> Score | None:
    exp = case.expected
    if not any([exp.tools_called, exp.tools_called_min is not None,
                exp.tools_called_max is not None, exp.tools_forbidden]):
        return None

    called = Counter(traj.tool_names)
    issues: list[str] = []

    # Required tools (each must appear at least once).
    if exp.tools_called:
        required = Counter(exp.tools_called)
        for tool, n in required.items():
            if called[tool] < n:
                issues.append(f"missing {tool} (called {called[tool]}× expected ≥{n}×)")

    # Forbidden tools.
    for tool in exp.tools_forbidden:
        if called.get(tool, 0) > 0:
            issues.append(f"forbidden tool called: {tool} ({called[tool]}×)")

    # Count bounds — total calls, including repeats.
    total = sum(called.values())
    if exp.tools_called_min is not None and total < exp.tools_called_min:
        issues.append(f"only {total} tool calls (min {exp.tools_called_min})")
    if exp.tools_called_max is not None and total > exp.tools_called_max:
        issues.append(f"{total} tool calls exceeds ceiling {exp.tools_called_max}")

    if issues:
        return Score(name="tool_selection", passed=False, detail="; ".join(issues))
    return Score(
        name="tool_selection",
        passed=True,
        detail=f"observed tools={dict(called)}",
    )
