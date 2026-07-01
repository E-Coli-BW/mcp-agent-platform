"""Scorer: lift — did our agent actually add value over a bare LLM call?

THE engineering-effect signal
-----------------------------
For cases that need tools, memory, or fresh data (the whole point of an
agent), a bare `chat.completions` call to the same underlying model
should FAIL while our agent SUCCEEDS. The gap between those two
outcomes is the value our engineering adds — and it's MODEL-INDEPENDENT
(swap Qwen for GPT-4 and the gap remains, if our engineering is sound).

This scorer is the cleanest answer to "are we testing the harness's
engineering effect, not the model's ability?" — yes, by explicitly
running the model TWICE on the same prompt:

  1. Through the full agent (with tools + memory + planner)
  2. Bare LLM call (no tools, no system prompt, no memory)

…and comparing the answers against the case's `final_must_contain` /
`final_must_not_contain` expectations.

  Lift outcomes:
    agent ✅, baseline ❌  → PASS  (agent added value: +1)
    agent ✅, baseline ✅  → PASS-NO-LIFT  (case is too easy — flag it)
    agent ❌, baseline ✅  → FAIL  (agent made things worse!)
    agent ❌, baseline ❌  → FAIL  (engineering didn't help)

A case with `expects_agent_lift: true` is FAILED unless the agent
strictly outperformed the baseline.

YAML usage
----------

    id: search_my_memory
    prompt: "What's my favorite color according to memory?"
    expected:
      expects_agent_lift: true              # baseline can't answer this — needs memory_search
      final_must_contain: ["blue"]

Configuration
-------------

  EVAL_BASELINE_MODEL    — Model for the bare call. Default = case.model.
  EVAL_BASELINE_BASE_URL — Default = OLLAMA at http://localhost:11434/v1
  EVAL_BASELINE_API_KEY  — Default "ollama"

The baseline call is made by the runner, not the scorer, because it
needs to happen *before* scoring (and only once per case). The scorer
just compares the two answers using the same substring rules as
`answer_grounded`.
"""
from __future__ import annotations

import re

from eval.case import Case, Score, Trajectory
from eval.scorers import register


def _answer_meets_expectations(answer: str, case: Case) -> tuple[bool, str]:
    """Apply the same `must_contain` / `must_not_contain` / `final_regex`
    rules `answer_grounded` uses — kept inline to avoid coupling the two
    scorers' module-private state.

    Returns (passed, reason). Reason is empty on pass.
    """
    if not answer:
        return False, "empty answer"
    low = answer.lower()
    for needle in case.expected.final_must_contain:
        if needle.lower() not in low:
            return False, f"missing '{needle}'"
    for needle in case.expected.final_must_not_contain:
        if needle.lower() in low:
            return False, f"contains forbidden '{needle}'"
    if case.expected.final_regex:
        if not re.search(case.expected.final_regex, answer):
            return False, f"regex {case.expected.final_regex!r} did not match"
    return True, ""


@register
def lift(case: Case, traj: Trajectory) -> Score | None:
    if not case.expected.expects_agent_lift:
        return None
    if traj.baseline_answer is None and traj.baseline_error is None:
        # Runner wasn't asked to gather baseline. Skip soft so the regular
        # `eval-run` doesn't trip on cases authored for lift mode.
        return None

    agent_passed, agent_reason = _answer_meets_expectations(traj.final_answer, case)

    if traj.baseline_error:
        # Baseline call failed → trivially agent has lift IF agent passed.
        if agent_passed:
            return Score(name="lift", passed=True,
                         detail=f"+lift: agent ✅, baseline transport-error ({traj.baseline_error[:60]})")
        return Score(name="lift", passed=False,
                     detail=f"-lift: agent ❌ ({agent_reason}); baseline also errored")

    baseline_passed, baseline_reason = _answer_meets_expectations(
        traj.baseline_answer or "", case
    )

    # Four-way outcome matrix
    if agent_passed and not baseline_passed:
        return Score(name="lift", passed=True,
                     detail=f"+lift: agent ✅, baseline ❌ ({baseline_reason})")
    if agent_passed and baseline_passed:
        # Bare LLM solved it too — case isn't engineering-discriminative.
        # Don't fail, but flag loudly so the case-author notices.
        return Score(name="lift", passed=True,
                     detail="⚠ no-lift: both agent and baseline passed; "
                            "case is too easy to attribute lift — consider "
                            "tightening or removing `expects_agent_lift`")
    if not agent_passed and baseline_passed:
        return Score(name="lift", passed=False,
                     detail=f"-lift REGRESSION: baseline ✅ but agent ❌ "
                            f"({agent_reason}) — engineering made it WORSE")
    return Score(name="lift", passed=False,
                 detail=f"-lift: agent ❌ ({agent_reason}); "
                        f"baseline ❌ ({baseline_reason}) — engineering didn't help")
