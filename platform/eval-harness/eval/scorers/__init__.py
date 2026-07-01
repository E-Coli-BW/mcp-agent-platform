"""Scorer registry.

A scorer is `(case, trajectory) -> Score | None`. Returning None means
"this scorer doesn't apply to this case" (e.g. tool-selection scorer
on a case with no `expected.tools_called` set) — the runner filters
those out so they don't pollute the aggregate.

Order matters: register cheap/deterministic first, expensive/flaky last.
The runner doesn't currently short-circuit on first failure (full
report > fast report), but the order affects how the report reads.
"""
from __future__ import annotations

from typing import Callable

from eval.case import Case, Score, Trajectory

Scorer = Callable[[Case, Trajectory], "Score | None"]

# Populated by importing the scorer modules below. Order = report order.
SCORERS: list[Scorer] = []


def register(scorer: Scorer) -> Scorer:
    """Decorator: register a scorer in the global list."""
    SCORERS.append(scorer)
    return scorer


# Import side-effects register the scorers.
from eval.scorers import (  # noqa: E402,F401  — registration via import
    transport,
    budgets,
    tool_selection,
    answer_grounded,
    efficiency,
    lift,
    llm_judge,
)
