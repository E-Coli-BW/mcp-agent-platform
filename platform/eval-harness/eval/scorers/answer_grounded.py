"""Scorer: answer_grounded — does the final answer mention required facts?

Case-insensitive substring checks. The intent is "did the answer cite
the value the agent supposedly retrieved?" — not "is the answer
beautifully written?". Beauty is the LLM-judge's job (separate scorer).

We also handle `final_must_not_contain` so cases can assert *negative*
properties: "answer must not contain 'I cannot' / 'error' / 'fail'".
And an optional `final_regex` for structural assertions like "answer
contains a number".
"""
from __future__ import annotations

import re

from eval.case import Case, Score, Trajectory
from eval.scorers import register


@register
def answer_grounded(case: Case, traj: Trajectory) -> Score | None:
    exp = case.expected
    if not any([exp.final_must_contain, exp.final_must_not_contain, exp.final_regex]):
        return None

    text = traj.final_answer or ""
    lower = text.lower()
    issues: list[str] = []

    for needle in exp.final_must_contain:
        if needle.lower() not in lower:
            issues.append(f"missing substring: {needle!r}")

    for forbidden in exp.final_must_not_contain:
        if forbidden.lower() in lower:
            issues.append(f"contains forbidden substring: {forbidden!r}")

    if exp.final_regex:
        # re.MULTILINE so anchors work line-by-line; re.IGNORECASE is the
        # default convention for our test answers (LLMs vary on case).
        if not re.search(exp.final_regex, text, re.MULTILINE | re.IGNORECASE):
            issues.append(f"regex did not match: {exp.final_regex!r}")

    if issues:
        # Truncate the answer to keep the report readable.
        snippet = text.replace("\n", " ")[:160]
        return Score(name="answer_grounded", passed=False,
                     detail=f"{'; '.join(issues)}  |  answer: {snippet!r}")
    return Score(name="answer_grounded", passed=True,
                 detail=f"all required substrings present ({len(exp.final_must_contain)})")
