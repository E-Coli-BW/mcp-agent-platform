"""Data model: Case, Trajectory, Score, RunResult.

These are the wire format between YAML files (golden cases), the
runner (which hits the agent and captures output), and scorers (which
turn a Trajectory into Score objects).

Kept deliberately small. Plain dataclasses, not pydantic, so the
harness has zero heavy runtime deps and YAML <-> Python round-trips
are obvious.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


# ── Case: what the YAML file declares ──────────────────────────────
@dataclass
class Expectations:
    """Machine-checkable expectations for a single case.

    Every field is OPTIONAL. The runner applies a scorer only if the
    relevant expectations field is set — so a case can opt in to just
    `final_must_contain` and skip tool-trace scoring entirely.
    """
    # Tool-trace expectations
    tools_called: list[str] = field(default_factory=list)         # exact set, order-independent
    tools_called_min: int | None = None                            # generous lower bound
    tools_called_max: int | None = None                            # catches runaway loops
    tools_forbidden: list[str] = field(default_factory=list)       # MUST NOT call (security)

    # Final-answer expectations
    final_must_contain: list[str] = field(default_factory=list)    # case-insensitive substring
    final_must_not_contain: list[str] = field(default_factory=list)
    final_regex: str | None = None                                 # additional regex constraint

    # Budget expectations (cost/perf — agent-quality intersection)
    max_latency_ms: int | None = None
    max_prompt_tokens: int | None = None
    max_completion_tokens: int | None = None

    # ── Engineering-effect scorers ────────────────────────────────
    # The "lift" scorer answers: did the agent layer add value on top
    # of a bare LLM call? Set `lift_required_for_baseline_fail=True` on
    # cases where the bare LLM is expected to FAIL but the agent should
    # succeed (typical: anything needing a tool, memory, or fresh data).
    #
    # When set, the runner makes a SECOND call to the same prompt against
    # the bare LLM (no tools, no memory, no agent prompt) and the scorer
    # compares outcomes. Cancels out model capability — measures what
    # OUR engineering added.
    expects_agent_lift: bool = False        # opt-in to lift scoring

    # Trajectory efficiency — catches "agent still passes but loops 3×".
    # Even on a passing case, if the agent suddenly uses 2× the tool
    # calls or tokens, that's a regression in OUR engineering (planner
    # got chattier, prompt got bloatier) — not the model.
    max_tool_calls_efficient: int | None = None    # tighter than tools_called_max
    max_total_tokens_efficient: int | None = None  # prompt + completion

    # ── n-of-k pass threshold (flakiness tolerance) ────────────────
    # The hard-won lesson of P1 #1/#2: a single eval run on a 7B model
    # at T>0 is a coin flip. We need n-of-k semantics:
    #
    #   "case passes if it passes >= min_pass_rate of runs"
    #
    # Default None ⇒ strict, every run must pass (back-compat).
    # Use 0.6 (3/5) for cases that are genuinely flaky on the chosen
    # model. Don't use this to paper over real bugs — only to model
    # the model's non-determinism.
    #
    # The runner's --runs flag controls N. This field controls the
    # acceptance threshold for THIS case specifically; CLI flag
    # --pass-threshold overrides it for every case.
    min_pass_rate: float | None = None

    # LLM-judge (slow, paid, last-resort)
    # Set `judge` to a dict like:
    #   judge:
    #     rubric: "Did the assistant explain WHY before showing code?"
    #     criteria: ["clarity", "correctness", "tone"]
    #     min_score: 4                          # out of 5
    #     samples: 3                            # self-consistency
    # The scorer is skipped entirely if `judge` is unset OR if the judge
    # backend isn't configured (no EVAL_JUDGE_MODEL env var).
    judge: dict[str, Any] | None = None


@dataclass
class Case:
    """One golden case loaded from `golden/*.yaml`."""
    id: str
    description: str
    prompt: str
    model: str = "qwen2.5:7b"                                      # default to local; can be overridden
    expected: Expectations = field(default_factory=Expectations)
    tags: list[str] = field(default_factory=list)                  # for filtering: ["fast", "memory"]
    # The session_id pattern lets us isolate cases — defaults to the
    # case id so every run uses a fresh agent session.
    session_id_pattern: str = "eval:{id}:{ts}"

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Case":
        exp = d.get("expected") or {}
        return cls(
            id=d["id"],
            description=d.get("description", ""),
            prompt=d["prompt"].rstrip("\n"),
            model=d.get("model", cls.model),
            expected=Expectations(**exp),
            tags=list(d.get("tags") or []),
            session_id_pattern=d.get("session_id_pattern", cls.session_id_pattern),
        )


# ── Trajectory: what the agent actually did ────────────────────────
@dataclass
class ToolCall:
    """One tool invocation observed in the trajectory."""
    tool: str
    input: dict[str, Any] = field(default_factory=dict)
    output: str = ""
    duration_ms: int | None = None                                 # if surfaced by the agent
    status: str = "ok"                                             # "ok" | "error"


@dataclass
class Trajectory:
    """Everything the runner captured for one case execution."""
    case_id: str
    prompt: str
    final_answer: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    # Raw envelope from /v1/chat/completions — kept for forensic reports.
    raw_response: dict = field(default_factory=dict)
    latency_ms: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    http_status: int = 0
    error: str | None = None                                       # transport-level errors
    # OPTIONAL: a paired bare-LLM baseline answer for the same prompt.
    # Populated when the runner is invoked with --with-baseline (or when
    # a case sets expects_agent_lift=True). Lets the `lift` scorer
    # compare "agent answer" vs "bare LLM answer" head-to-head.
    baseline_answer: str | None = None
    baseline_latency_ms: int | None = None
    baseline_error: str | None = None

    @property
    def tool_names(self) -> list[str]:
        return [tc.tool for tc in self.tool_calls]


# ── Score: what a scorer emits per case ────────────────────────────
@dataclass
class Score:
    """Result of one scorer applied to one trajectory."""
    name: str                                                       # "tool_selection", "answer_grounded", ...
    passed: bool
    detail: str                                                     # human-readable explanation
    weight: float = 1.0                                             # used in pass_rate aggregation


# ── RunResult: per-case outcome rolled up across scorers ───────────
@dataclass
class RunResult:
    case: Case
    trajectory: Trajectory
    scores: list[Score] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        """A case passes only if ALL applicable scorers passed.

        This is intentionally strict — partial credit hides regressions.
        A scorer that doesn't apply (e.g. no `final_must_contain` on the
        case) is simply not emitted, so it doesn't count.
        """
        return all(s.passed for s in self.scores) and self.trajectory.error is None

    def to_dict(self) -> dict:
        return {
            "case_id": self.case.id,
            "passed": self.passed,
            "scores": [
                {"name": s.name, "passed": s.passed, "detail": s.detail, "weight": s.weight}
                for s in self.scores
            ],
            "latency_ms": self.trajectory.latency_ms,
            "prompt_tokens": self.trajectory.prompt_tokens,
            "completion_tokens": self.trajectory.completion_tokens,
            "tool_calls": self.trajectory.tool_names,
            "baseline_latency_ms": self.trajectory.baseline_latency_ms,
            "baseline_answer_excerpt": (self.trajectory.baseline_answer or "")[:300] or None,
            "error": self.trajectory.error,
        }


# ── CaseAggregate: n-of-k roll-up ──────────────────────────────────
@dataclass
class CaseAggregate:
    """Aggregate of N independent RunResults for ONE case.

    Rationale: a single LLM-in-the-loop verdict on a 7B model at T>0
    is a coin flip; only k/N pass-rates are evidence.

    Acceptance contract:
      - if N == 1: passed = runs[0].passed (back-compat, indistinguishable
        from old single-run RunResult flow).
      - if N > 1: passed iff (passed_count / N) >= effective_threshold.

    The threshold is resolved at construction time so the aggregator
    doesn't need a settings/CLI handle. Priority (highest first):
      1. CLI --pass-threshold (passed in as cli_pass_threshold)
      2. Per-case `expected.min_pass_rate`
      3. Default 1.0 (strict, every run must pass).
    """
    case: Case
    runs: list[RunResult]
    cli_pass_threshold: float | None = None

    @property
    def n(self) -> int:
        return len(self.runs)

    @property
    def passed_count(self) -> int:
        return sum(1 for r in self.runs if r.passed)

    @property
    def pass_rate(self) -> float:
        return (self.passed_count / self.n) if self.n else 0.0

    @property
    def effective_threshold(self) -> float:
        if self.cli_pass_threshold is not None:
            return self.cli_pass_threshold
        if self.case.expected.min_pass_rate is not None:
            return self.case.expected.min_pass_rate
        return 1.0

    @property
    def passed(self) -> bool:
        if not self.runs:
            return False
        # `>=` not `>` so threshold 1.0 means "all runs pass".
        return self.pass_rate >= self.effective_threshold

    def to_dict(self) -> dict:
        """Summary view — one entry per case, hides per-run noise.

        Compatible with the existing CI gate (which keys off `case_id`
        and `passed`). Adds n-of-k context fields that older consumers
        ignore.
        """
        first = self.runs[0] if self.runs else None
        # Aggregate latency / token stats across runs (means; min/max
        # would be more informative but the summary view is meant to be
        # a one-line scan, not full stats).
        def _mean(values: list[int]) -> int:
            return int(sum(values) / len(values)) if values else 0
        latencies = [r.trajectory.latency_ms for r in self.runs]
        prompt_toks = [r.trajectory.prompt_tokens for r in self.runs]
        completion_toks = [r.trajectory.completion_tokens for r in self.runs]
        # Tool names: take the first run's set as representative — in
        # practice, the agent tends to make the same tool choices across
        # runs even when it produces different final text.
        tool_names = first.trajectory.tool_names if first else []
        return {
            "case_id": self.case.id,
            "passed": self.passed,
            "pass_rate": round(self.pass_rate, 3),
            "passed_count": self.passed_count,
            "runs": self.n,
            "threshold": self.effective_threshold,
            # One representative scores entry from the first run, so the
            # report can still show per-scorer pass/fail without exploding
            # to N × scorer rows. Use the first run; per-run detail is in
            # `trajectories.jsonl`.
            "scores": (
                [
                    {"name": s.name, "passed": s.passed, "detail": s.detail, "weight": s.weight}
                    for s in first.scores
                ]
                if first
                else []
            ),
            "latency_ms": _mean(latencies),
            "prompt_tokens": _mean(prompt_toks),
            "completion_tokens": _mean(completion_toks),
            "tool_calls": tool_names,
            "baseline_latency_ms": (
                first.trajectory.baseline_latency_ms if first else None
            ),
            "baseline_answer_excerpt": (
                (first.trajectory.baseline_answer or "")[:300] or None
            ) if first else None,
            "error": first.trajectory.error if first else None,
        }


# ── Helpers ────────────────────────────────────────────────────────
_TOOL_MARKER_RE = re.compile(
    r"<!--\s*TOOL:(?P<json>\{.*?\})\s*-->",
    re.DOTALL,
)

def parse_tool_markers(content: str) -> list[dict[str, Any]]:
    """Extract `<!-- TOOL:{...} -->` markers from agent response content.

    The agent emits these alongside the natural-language answer so that
    a downstream consumer (chat UI, eval harness) can reconstruct what
    happened without parsing the LLM's prose. We rely on them as the
    primary source of tool-trace truth.

    Returns a list of dicts (best-effort; malformed JSON markers are
    skipped — that's a bug for the agent team to fix, not the eval to
    crash on).
    """
    import json
    out: list[dict[str, Any]] = []
    for m in _TOOL_MARKER_RE.finditer(content):
        try:
            out.append(json.loads(m.group("json")))
        except json.JSONDecodeError:
            continue
    return out
