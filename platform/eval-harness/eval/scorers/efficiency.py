"""Scorer: efficiency — engineering effect, ignoring model capability.

Why this exists
---------------
Even if the agent gives the right answer, it can do so wastefully:
loop the planner 3× when it should loop once, send a 4KB system prompt
when 1KB would do, retry a working tool just in case. Those are pure
ENGINEERING regressions — the model's perfectly capable, our wiring got
bloated.

This scorer is opt-in and INDEPENDENT of model quality. A 7B model and
a GPT-4 model running the SAME agent code should produce roughly the
same tool-call count and similar prompt token counts (the prompt is OUR
template, the tool calls are OUR planner). If those numbers double on a
green run, something in our code drifted.

When to use it
--------------
On every "happy path" case where you know the ideal trajectory shape.
Don't bother with cases that legitimately need variable-length retries.

YAML usage
----------

    expected:
      max_tool_calls_efficient: 2       # was 2 in the green baseline; alarm at 3+
      max_total_tokens_efficient: 1500  # was ~1200 on master; alarm above 1500

Both fields are opt-in. The scorer returns None if neither is set.
"""
from __future__ import annotations

from eval.case import Case, Score, Trajectory
from eval.scorers import register


@register
def efficiency(case: Case, traj: Trajectory) -> Score | None:
    cap_calls = case.expected.max_tool_calls_efficient
    cap_tokens = case.expected.max_total_tokens_efficient
    if cap_calls is None and cap_tokens is None:
        return None                              # opt-out

    actual_calls = len(traj.tool_calls)
    actual_tokens = (traj.prompt_tokens or 0) + (traj.completion_tokens or 0)

    fails: list[str] = []
    pieces: list[str] = []

    if cap_calls is not None:
        pieces.append(f"tool_calls={actual_calls}≤{cap_calls}")
        if actual_calls > cap_calls:
            fails.append(f"tool_calls={actual_calls} > efficient_cap={cap_calls}")

    if cap_tokens is not None:
        pieces.append(f"tokens={actual_tokens}≤{cap_tokens}")
        if actual_tokens > cap_tokens:
            fails.append(f"tokens={actual_tokens} > efficient_cap={cap_tokens}")

    if fails:
        return Score(name="efficiency", passed=False,
                     detail="; ".join(fails))
    return Score(name="efficiency", passed=True,
                 detail="; ".join(pieces))
