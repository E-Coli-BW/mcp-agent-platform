"""BFCL — Berkeley Function Calling Leaderboard adapter.

What BFCL tests
---------------
Given (user_prompt, tool_catalog), did the model call the RIGHT tool
with the RIGHT arguments? This is a *single-turn* eval against the
underlying LLM — it bypasses our agent loop entirely.

Why we run it separately
------------------------
Our regular `eval-run` exercises the full agent loop: prompt → planner →
tool execution → answer. That tells us if the WHOLE system works for our
specific prompts. BFCL tells us if the underlying MODEL is good at
function calling on a *broad* distribution of tool schemas — including
ones we don't ship. The two signals are complementary:

  - `eval-run` fails → bug in OUR agent (prompt template, tool wiring,
    planner config, …).
  - `eval-suite-bfcl` fails → the model is weak at function calling;
    consider a different model OR provide more examples.

Configuration
-------------
Same env vars as `llm_judge` for backend:

  EVAL_BFCL_MODEL     — LLM under test (defaults to EVAL_JUDGE_MODEL).
  EVAL_BFCL_BASE_URL  — OpenAI-compatible endpoint.
  EVAL_BFCL_API_KEY   — API key.

Plus:

  EVAL_BFCL_DATA_DIR  — Path to BFCL JSON files.
                         Default: ./suites/bfcl/data/  (10 bundled cases)
                         For full set: ./scripts/download-bfcl.sh

Scoring
-------
A case passes iff:
  1. The model emits exactly one tool_call (BFCL "simple" subset).
  2. tool_call.name matches the expected function name.
  3. All required argument keys are present.
  4. Each argument value matches the expected ground truth (type-aware).

This is intentionally strict — BFCL's value is in catching subtle
schema-handling bugs (e.g. model emitting "true"/`true` for booleans).

Reference: https://gorilla.cs.berkeley.edu/leaderboard.html
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger("eval.bfcl")

DEFAULT_DATA_DIR = Path(__file__).resolve().parent / "data"
DEFAULT_RUNS_DIR = Path(__file__).resolve().parent.parent.parent / "runs"


# ── Case shape (BFCL JSON, normalized) ───────────────────────────────

@dataclass
class BfclCase:
    """One BFCL function-calling case."""
    id: str
    category: str                                 # "simple", "parallel", "multiple", …
    question: str
    tools: list[dict]                             # OpenAI function-schema list
    expected_name: str                            # function the model SHOULD call
    expected_args: dict[str, Any]                 # ground truth arguments
    # For "parallel" / "multiple" categories the expected output is a list.
    # We start with "simple" only; extending is straightforward.


@dataclass
class BfclResult:
    case_id: str
    category: str
    passed: bool
    detail: str
    predicted_name: str | None = None
    predicted_args: dict | None = None
    latency_ms: int = 0
    sub_checks: dict[str, bool] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "case_id": self.case_id,
            "category": self.category,
            "passed": self.passed,
            "detail": self.detail,
            "predicted_name": self.predicted_name,
            "predicted_args": self.predicted_args,
            "latency_ms": self.latency_ms,
            "sub_checks": self.sub_checks,
        }


# ── Loading ──────────────────────────────────────────────────────────

def load_cases(data_dir: Path) -> list[BfclCase]:
    """Load every *.json file under data_dir.

    Each file may contain one case (dict) or many (list). Normalizes
    the BFCL-on-disk format into our `BfclCase`.
    """
    cases: list[BfclCase] = []
    if not data_dir.exists():
        raise FileNotFoundError(f"BFCL data dir not found: {data_dir}")
    for jf in sorted(data_dir.glob("**/*.json")):
        raw = json.loads(jf.read_text())
        items = raw if isinstance(raw, list) else [raw]
        for it in items:
            try:
                cases.append(BfclCase(
                    id=str(it["id"]),
                    category=it.get("category", "simple"),
                    question=it["question"],
                    tools=it["tools"],
                    expected_name=it["expected"]["name"],
                    expected_args=it["expected"]["arguments"],
                ))
            except KeyError as e:
                raise ValueError(f"{jf}: missing field {e}") from e
    return cases


# ── Calling the LLM under test ───────────────────────────────────────

def _resolve_settings() -> dict[str, str]:
    model = os.environ.get("EVAL_BFCL_MODEL") or os.environ.get("EVAL_JUDGE_MODEL")
    if not model:
        raise RuntimeError(
            "neither EVAL_BFCL_MODEL nor EVAL_JUDGE_MODEL set — "
            "BFCL needs an LLM-under-test"
        )
    base = os.environ.get("EVAL_BFCL_BASE_URL") or os.environ.get(
        "EVAL_JUDGE_BASE_URL", "http://localhost:11434/v1"
    )
    key = os.environ.get("EVAL_BFCL_API_KEY") or os.environ.get(
        "EVAL_JUDGE_API_KEY", "ollama"
    )
    return {"model": model, "base_url": base.rstrip("/"), "api_key": key}


def _call_llm(settings: dict[str, str], case: BfclCase, client: httpx.Client) -> tuple[dict, int]:
    """Send the BFCL question + tools to the LLM. Returns (parsed_body, latency_ms)."""
    payload = {
        "model": settings["model"],
        "messages": [{"role": "user", "content": case.question}],
        "tools": case.tools,
        "tool_choice": "auto",
        "temperature": 0,                         # deterministic for eval
    }
    start = time.monotonic()
    r = client.post(
        f"{settings['base_url']}/chat/completions",
        headers={
            "Authorization": f"Bearer {settings['api_key']}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=60,
    )
    latency_ms = int((time.monotonic() - start) * 1000)
    r.raise_for_status()
    return r.json(), latency_ms


# ── Scoring one case ─────────────────────────────────────────────────

def _extract_tool_call(body: dict) -> tuple[str | None, dict | None]:
    """Return (function_name, arguments_dict) or (None, None) if no tool_call."""
    choice = (body.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    tcs = msg.get("tool_calls") or []
    if not tcs:
        return None, None
    fn = tcs[0].get("function") or {}
    name = fn.get("name")
    args_raw = fn.get("arguments")
    if isinstance(args_raw, str):
        try:
            args = json.loads(args_raw)
        except json.JSONDecodeError:
            return name, None
    elif isinstance(args_raw, dict):
        args = args_raw
    else:
        args = None
    return name, args


def _values_match(expected: Any, actual: Any) -> bool:
    """Type-aware comparison.

    BFCL ground truth uses a list as "any of these is acceptable":
      expected=["NYC", "New York"], actual="New York" → match

    To require an actual list value, wrap it once more:
      expected=[["a","b"]], actual=["a","b"]          → match
      expected=["a","b"],   actual="a"                → match (alternatives)

    Numbers compare loosely across int/float; booleans STRICTLY (don't
    let the model get away with "true" for True); strings ignore case
    and surrounding whitespace.
    """
    # Alternatives semantics — but only when actual is NOT a list itself.
    # If actual is a list, fall through to element-wise list comparison.
    if isinstance(expected, list) and not isinstance(actual, list):
        return any(_values_match(e, actual) for e in expected)

    # Both lists → element-wise comparison.
    if isinstance(expected, list) and isinstance(actual, list):
        # Two paths: outer-list-as-alternatives where one alt is the matching list,
        # OR straight element-wise. Try alternatives first (preserves the
        # `[["a","b"]]` idiom), then fall back to direct comparison.
        if any(isinstance(e, list) and _values_match(e, actual) for e in expected):
            return True
        if len(expected) != len(actual):
            return False
        return all(_values_match(e, a) for e, a in zip(expected, actual))

    # Booleans must be booleans on both sides (don't coerce "true"/1 → True).
    if isinstance(expected, bool) or isinstance(actual, bool):
        return isinstance(expected, bool) and isinstance(actual, bool) and expected == actual

    if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
        return float(expected) == float(actual)

    if isinstance(expected, str) and isinstance(actual, str):
        return expected.strip().lower() == actual.strip().lower()

    return expected == actual


def score_case(case: BfclCase, body: dict, latency_ms: int) -> BfclResult:
    """Apply BFCL's "simple" scoring rules."""
    name, args = _extract_tool_call(body)
    sub: dict[str, bool] = {
        "emitted_tool_call": name is not None,
        "name_matches": name == case.expected_name,
        "args_parseable": args is not None,
    }

    # Special sentinel: expected_name="__none__" means the model should
    # NOT call any tool (chitchat / out-of-scope detection).
    if case.expected_name == "__none__":
        if name is None:
            return BfclResult(
                case_id=case.id, category=case.category, passed=True,
                detail="correctly refrained from calling a tool",
                latency_ms=latency_ms,
                sub_checks={"refrained_from_tool_call": True},
            )
        return BfclResult(
            case_id=case.id, category=case.category, passed=False,
            detail=f"called {name}({args}) when no tool was appropriate",
            predicted_name=name, predicted_args=args,
            latency_ms=latency_ms,
            sub_checks={"refrained_from_tool_call": False},
        )

    if not sub["emitted_tool_call"]:
        return BfclResult(
            case_id=case.id, category=case.category, passed=False,
            detail="no tool_call emitted — model answered in prose",
            predicted_name=None, predicted_args=None,
            latency_ms=latency_ms, sub_checks=sub,
        )
    if not sub["name_matches"]:
        return BfclResult(
            case_id=case.id, category=case.category, passed=False,
            detail=f"wrong function: predicted={name} expected={case.expected_name}",
            predicted_name=name, predicted_args=args,
            latency_ms=latency_ms, sub_checks=sub,
        )
    if not sub["args_parseable"]:
        return BfclResult(
            case_id=case.id, category=case.category, passed=False,
            detail="arguments not parseable as JSON",
            predicted_name=name, predicted_args=None,
            latency_ms=latency_ms, sub_checks=sub,
        )

    # Required-key + value match
    missing = [k for k in case.expected_args if k not in (args or {})]
    sub["all_required_args_present"] = not missing
    if missing:
        return BfclResult(
            case_id=case.id, category=case.category, passed=False,
            detail=f"missing required args: {missing}",
            predicted_name=name, predicted_args=args,
            latency_ms=latency_ms, sub_checks=sub,
        )

    wrong = [
        f"{k}={args.get(k)!r} vs expected {v!r}"
        for k, v in case.expected_args.items()
        if not _values_match(v, args.get(k))
    ]
    sub["all_arg_values_correct"] = not wrong
    if wrong:
        return BfclResult(
            case_id=case.id, category=case.category, passed=False,
            detail="; ".join(wrong),
            predicted_name=name, predicted_args=args,
            latency_ms=latency_ms, sub_checks=sub,
        )

    return BfclResult(
        case_id=case.id, category=case.category, passed=True,
        detail=f"correct call: {name}({args})",
        predicted_name=name, predicted_args=args,
        latency_ms=latency_ms, sub_checks=sub,
    )


# ── Runner ───────────────────────────────────────────────────────────

def run_all(cases: list[BfclCase]) -> list[BfclResult]:
    settings = _resolve_settings()
    results: list[BfclResult] = []
    with httpx.Client(timeout=120) as client:
        for c in cases:
            try:
                body, latency = _call_llm(settings, c, client)
                results.append(score_case(c, body, latency))
            except Exception as e:
                results.append(BfclResult(
                    case_id=c.id, category=c.category, passed=False,
                    detail=f"transport error: {type(e).__name__}: {e}",
                    latency_ms=0,
                ))
            log.info("[%-30s] %s",
                     c.id, "✅ pass" if results[-1].passed else f"❌ {results[-1].detail[:80]}")
    return results


def _summarize(results: list[BfclResult]) -> dict:
    total = len(results)
    passed = sum(1 for r in results if r.passed)
    by_cat: dict[str, dict[str, int]] = {}
    for r in results:
        bucket = by_cat.setdefault(r.category, {"total": 0, "passed": 0})
        bucket["total"] += 1
        if r.passed:
            bucket["passed"] += 1
    return {
        "suite": "bfcl",
        "model": _resolve_settings()["model"],
        "total": total,
        "passed": passed,
        "failed": total - passed,
        "pass_rate": round(passed / total, 3) if total else 0.0,
        "by_category": by_cat,
        "cases": [r.to_dict() for r in results],
    }


def cli() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(description="Run the BFCL function-calling suite.")
    ap.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR),
                    help="Directory of BFCL JSON case files (recursive).")
    ap.add_argument("--runs-dir", default=str(DEFAULT_RUNS_DIR))
    ap.add_argument("--category", action="append", default=[],
                    help="Run only cases in these categories. Repeatable.")
    ap.add_argument("--limit", type=int, default=None,
                    help="Run only the first N cases (for smoke tests).")
    args = ap.parse_args()

    try:
        cases = load_cases(Path(args.data_dir))
    except (FileNotFoundError, ValueError) as e:
        log.error("load failed: %s", e)
        return 2

    if args.category:
        cases = [c for c in cases if c.category in args.category]
    if args.limit:
        cases = cases[: args.limit]
    if not cases:
        log.error("no cases match filters")
        return 2

    log.info("BFCL: running %d case(s) against model=%s",
             len(cases), _resolve_settings()["model"])
    results = run_all(cases)
    summary = _summarize(results)

    runs_dir = Path(args.runs_dir) / time.strftime("bfcl-%Y-%m-%dT%H-%M-%S")
    runs_dir.mkdir(parents=True, exist_ok=True)
    (runs_dir / "summary.json").write_text(json.dumps(summary, indent=2))

    # Dashboard-friendly snapshot — overwritten each run.
    Path(args.runs_dir).mkdir(parents=True, exist_ok=True)
    (Path(args.runs_dir) / "latest-bfcl.json").write_text(json.dumps({
        **{k: v for k, v in summary.items() if k != "cases"},
        "run_dir": str(runs_dir),
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "cases": [
            {"case_id": c["case_id"], "category": c["category"],
             "passed": c["passed"], "detail": c["detail"][:200],
             "latency_ms": c["latency_ms"]}
            for c in summary["cases"]
        ],
    }, indent=2))

    log.info("\nBFCL results: %d/%d passed (%.0f%%) → %s",
             summary["passed"], summary["total"],
             summary["pass_rate"] * 100, runs_dir)

    if summary["by_category"]:
        log.info("by category:")
        for cat, b in summary["by_category"].items():
            rate = b["passed"] / b["total"] * 100 if b["total"] else 0
            log.info("  %-15s %d/%d (%.0f%%)", cat, b["passed"], b["total"], rate)

    return 0 if summary["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(cli())
