"""Scorer: transport — did the HTTP call even succeed?

Always applies. If this scorer fails, every later scorer is irrelevant
(no trajectory to analyze) — but the runner still emits them as
"not-applicable" with detail referencing the transport failure, so the
report is unambiguous.
"""
from __future__ import annotations

from eval.case import Case, Score, Trajectory
from eval.scorers import register


@register
def transport(case: Case, traj: Trajectory) -> Score | None:
    if traj.error:
        return Score(name="transport", passed=False,
                     detail=f"transport error: {traj.error}")
    if traj.http_status and traj.http_status != 200:
        return Score(name="transport", passed=False,
                     detail=f"HTTP {traj.http_status}")
    return Score(name="transport", passed=True,
                 detail=f"HTTP 200 in {traj.latency_ms}ms")
