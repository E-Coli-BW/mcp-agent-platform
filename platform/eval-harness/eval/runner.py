"""Runner: load YAML cases, hit the agent, score the trajectories.

Two entrypoints:

  - `run_all(cases, agent_url, jwt)` — programmatic; used by tests
    and by the CLI.
  - `cli()`            — `python -m eval.runner` or `eval-run` script;
                         arg-parses, loads cases, runs, writes a report.

Why call the live agent instead of mocking the LLM?
-------------------------------------------------
Mocking the LLM at the wire level (Ollama / OpenAI) means we don't
exercise the actual prompt template, tool registration, or tool
execution paths — which is where most agent bugs live. If we mock the
LLM we're testing our mock, not the agent.

The tradeoff: eval runs need a live stack (`scripts/dev/scenarios/
with-tools/up.sh`). CI runs spin up the stack as a setup step.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Iterable

import httpx
import yaml

from eval.case import Case, RunResult, ToolCall, Trajectory, CaseAggregate, parse_tool_markers
from eval.scorers import SCORERS

log = logging.getLogger("eval.runner")


# ── Config ───────────────────────────────────────────────────────────
DEFAULT_AGENT_URL = os.environ.get("EVAL_AGENT_URL", "http://localhost:8580")
DEFAULT_AUTH_URL = os.environ.get("EVAL_AUTH_URL", "http://localhost:8090")
DEFAULT_JWT_FILE = os.environ.get(
    "EVAL_JWT_FILE", str(Path.cwd().parent.parent / ".dev-run" / "jwt.txt")
)
DEFAULT_CASES_DIR = Path(__file__).resolve().parent.parent / "golden"
DEFAULT_RUNS_DIR = Path(__file__).resolve().parent.parent / "runs"
HTTP_TIMEOUT = float(os.environ.get("EVAL_HTTP_TIMEOUT", "120"))

# ── Baseline (bare-LLM) call for the `lift` scorer ──────────────────
# Hit the model DIRECTLY (no agent, no tools, no system prompt) with the
# same prompt. This factors out model capability — the lift scorer
# compares agent-pass vs baseline-pass.
DEFAULT_BASELINE_BASE_URL = os.environ.get(
    "EVAL_BASELINE_BASE_URL", "http://localhost:11434/v1"
)
DEFAULT_BASELINE_API_KEY = os.environ.get("EVAL_BASELINE_API_KEY", "ollama")


# ── Loading cases ────────────────────────────────────────────────────
def load_cases(path: Path | str) -> list[Case]:
    """Load every *.yaml file under `path` (recursively)."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"cases dir not found: {p}")
    cases: list[Case] = []
    for yml in sorted(p.glob("**/*.yaml")):
        with yml.open() as fh:
            raw = yaml.safe_load(fh)
        # Each file MAY contain a single case or a list of cases.
        items = raw if isinstance(raw, list) else [raw]
        for item in items:
            try:
                cases.append(Case.from_dict(item))
            except Exception as e:
                raise ValueError(f"{yml}: bad case definition: {e}") from e
    return cases


def _ensure_jwt(jwt_file: str, auth_url: str) -> str:
    """Read JWT from file; if missing/expired, mint a new one.

    Uses the same idempotent "signup-or-login as user `test`" recipe as
    `scripts/dev/common.sh::ensure_test_user` so eval and ad-hoc dev
    work share one identity.
    """
    p = Path(jwt_file)
    if p.exists():
        existing = p.read_text().strip()
        if existing:
            return existing
    log.info("minting fresh JWT (file empty/missing: %s)", jwt_file)
    with httpx.Client(timeout=10) as c:
        # signup is idempotent — ignore 409/400.
        try:
            c.post(
                f"{auth_url}/auth/signup",
                json={"username": "test", "password": "testpass1234",
                      "tenant_id": "default", "email": "test@test.com"},
            )
        except httpx.HTTPError:
            pass
        r = c.post(
            f"{auth_url}/auth/login",
            json={"username": "test", "password": "testpass1234"},
        )
        r.raise_for_status()
        jwt = r.json()["access_token"]
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(jwt)
    return jwt


# ── Hitting the agent ────────────────────────────────────────────────
async def _run_one(client: httpx.AsyncClient, agent_url: str, jwt: str,
                   case: Case, run_idx: int = 0) -> Trajectory:
    """Single chat-completion turn for one case.

    `run_idx` is appended to the session id so the N-of-k repeat loop
    gets N independent agent sessions (no checkpoint bleed between runs
    of the same case).
    """
    # `run_idx` participates in the session id so each repeat is independent.
    base_id = f"{case.id}:{run_idx}" if run_idx else case.id
    session_id = case.session_id_pattern.format(id=base_id, ts=int(time.time()))
    payload = {
        "model": case.model,
        "messages": [{"role": "user", "content": case.prompt}],
        "stream": False,
        # Distinct session so checkpoint state from a prior case doesn't bleed.
        "session_id": session_id,
        # We request deterministic decoding here, but be aware:
        # chat.py accepts `temperature` in the body but does NOT currently
        # plumb it through to the chat model (which is hard-coded to 0.7 in
        # graph.py::_create_chat_model). So today this is aspirational —
        # see eval skill for the flake-tolerant n-of-k retry pattern as a
        # workaround until graph.py honors the request-level temperature.
        "temperature": 0,
    }
    start = time.monotonic()
    try:
        r = await client.post(
            f"{agent_url}/v1/chat/completions",
            headers={"Authorization": f"Bearer {jwt}",
                     "Content-Type": "application/json"},
            json=payload,
        )
        latency_ms = int((time.monotonic() - start) * 1000)
    except Exception as e:
        return Trajectory(
            case_id=case.id, prompt=case.prompt, final_answer="",
            latency_ms=int((time.monotonic() - start) * 1000),
            http_status=0, error=f"{type(e).__name__}: {e}",
        )

    try:
        body = r.json()
    except Exception:
        return Trajectory(
            case_id=case.id, prompt=case.prompt, final_answer=r.text[:500],
            latency_ms=latency_ms, http_status=r.status_code,
            error=f"non-JSON response: {r.text[:200]}",
        )

    if r.status_code != 200:
        return Trajectory(
            case_id=case.id, prompt=case.prompt,
            final_answer=json.dumps(body)[:500],
            latency_ms=latency_ms, http_status=r.status_code,
            raw_response=body, error=f"HTTP {r.status_code}",
        )

    choice = (body.get("choices") or [{}])[0]
    content = (choice.get("message") or {}).get("content", "") or ""
    markers = parse_tool_markers(content)

    # Pair {action:start}/{action:end} markers into ToolCall objects.
    # Many agents emit only "start" markers (we don't have a result echo),
    # in which case we still record the call with empty output.
    tool_calls: list[ToolCall] = []
    pending: dict[str, ToolCall] = {}
    for m in markers:
        tool = m.get("tool", "?")
        action = m.get("action", "start")
        if action == "start":
            tc = ToolCall(tool=tool, input=m.get("input") or {})
            tool_calls.append(tc)
            pending[tool] = tc                    # latest call wins (good enough)
        elif action in ("end", "result", "complete"):
            tc = pending.pop(tool, None) or tool_calls[-1] if tool_calls else None
            if tc is not None:
                tc.output = str(m.get("output", ""))[:500]
                if "duration_ms" in m:
                    tc.duration_ms = int(m["duration_ms"])
                tc.status = m.get("status", "ok")

    usage = body.get("usage") or {}
    # Final answer = the assistant content with tool markers stripped, so
    # `answer_grounded` matches against the user-visible text.
    import re as _re
    final = _re.sub(r"<!--\s*TOOL:.*?-->", "", content, flags=_re.DOTALL).strip()

    return Trajectory(
        case_id=case.id, prompt=case.prompt, final_answer=final,
        tool_calls=tool_calls, raw_response=body, latency_ms=latency_ms,
        prompt_tokens=int(usage.get("prompt_tokens") or 0),
        completion_tokens=int(usage.get("completion_tokens") or 0),
        http_status=200,
    )


def _score_trajectory(case: Case, traj: Trajectory) -> RunResult:
    """Apply every registered scorer; drop the ones that returned None."""
    scores = []
    for scorer in SCORERS:
        s = scorer(case, traj)
        if s is not None:
            scores.append(s)
    return RunResult(case=case, trajectory=traj, scores=scores)


# ── Bare-LLM baseline (the "engineering-effect" half of the lift scorer) ─
async def _run_baseline(client: httpx.AsyncClient, case: Case) -> tuple[str | None, int | None, str | None]:
    """Call the underlying LLM with NO agent loop, tools, or system prompt.

    Returns (answer, latency_ms, error). The lift scorer uses this to
    compare against the full agent's answer on the same prompt — that's
    how we isolate "engineering effect" from "model capability".
    """
    model = os.environ.get("EVAL_BASELINE_MODEL", case.model)
    base = DEFAULT_BASELINE_BASE_URL.rstrip("/")
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": case.prompt}],
        "temperature": 0,                         # deterministic baseline
        "stream": False,
    }
    start = time.monotonic()
    try:
        r = await client.post(
            f"{base}/chat/completions",
            headers={
                "Authorization": f"Bearer {DEFAULT_BASELINE_API_KEY}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=HTTP_TIMEOUT,
        )
        latency_ms = int((time.monotonic() - start) * 1000)
        r.raise_for_status()
        body = r.json()
        content = ((body.get("choices") or [{}])[0].get("message") or {}).get("content", "") or ""
        return content.strip(), latency_ms, None
    except Exception as e:
        return None, int((time.monotonic() - start) * 1000), f"{type(e).__name__}: {e}"


async def run_all(cases: Iterable[Case], agent_url: str, jwt: str,
                  concurrency: int = 1,
                  with_baseline: bool = False,
                  runs_per_case: int = 1,
                  cli_pass_threshold: float | None = None) -> list[CaseAggregate]:
    """Execute every case against the agent and score.

    `concurrency=1` by default — agent state (sessions, checkpoints) is
    sticky and running cases in parallel hides bugs. Crank up only for
    perf eval.

    `with_baseline=True` also makes a bare-LLM call per case (no agent,
    no tools) so the `lift` scorer can compare them. Cases that don't
    opt in via `expects_agent_lift` don't pay the cost.

    `runs_per_case` — n-of-k flakiness tolerance. The case is run N
    times sequentially with distinct session ids; the resulting
    CaseAggregate is `passed` iff the per-case threshold is met.
    Sequential (not parallel) because the agent caches a single
    (model, settings) → graph instance and we want to be sure we're
    measuring *non-determinism in the model*, not parallel-execution
    artefacts. Baseline is called ONCE per case (deterministic at T=0)
    and shared across runs to avoid N× baseline cost.

    `cli_pass_threshold` — global override for the per-case
    `expected.min_pass_rate`. None ⇒ honour per-case thresholds (or
    default 1.0). Useful for "let me see what pass rate I'm at" runs.
    """
    sem = asyncio.Semaphore(concurrency)

    async def _go(case: Case, client: httpx.AsyncClient) -> CaseAggregate:
        async with sem:
            # ── Baseline: call once per case, share across N agent runs ──
            # The baseline uses T=0 so it's deterministic; running it N
            # times would just spend N× the tokens for an identical
            # answer. The lift scorer compares each agent run against
            # this single baseline.
            shared_baseline_answer: str | None = None
            shared_baseline_latency: int | None = None
            shared_baseline_error: str | None = None

            need_baseline = with_baseline or case.expected.expects_agent_lift
            if need_baseline:
                ans, lat, err = await _run_baseline(client, case)
                shared_baseline_answer = ans
                shared_baseline_latency = lat
                shared_baseline_error = err

            # ── N independent agent runs ──
            run_results: list[RunResult] = []
            for run_idx in range(runs_per_case):
                traj = await _run_one(client, agent_url, jwt, case, run_idx=run_idx)
                if need_baseline and traj.error is None:
                    traj.baseline_answer = shared_baseline_answer
                    traj.baseline_latency_ms = shared_baseline_latency
                    traj.baseline_error = shared_baseline_error
                run_results.append(_score_trajectory(case, traj))

            return CaseAggregate(
                case=case,
                runs=run_results,
                cli_pass_threshold=cli_pass_threshold,
            )

    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        return await asyncio.gather(*[_go(c, client) for c in cases])


# ── CLI ──────────────────────────────────────────────────────────────
def _summarize(aggregates: list[CaseAggregate]) -> dict:
    """Roll N CaseAggregates into one dashboard-friendly summary.

    Each `cases[i].passed` is the *n-of-k* verdict, not a single-run
    one — see CaseAggregate. The CI gate keys off this boolean, so
    flipping a case to `min_pass_rate: 0.6` and running with --runs 5
    will make it pass the gate as long as 3/5 runs pass.
    """
    total = len(aggregates)
    passed = sum(1 for a in aggregates if a.passed)

    # Roll up per-scorer pass/fail counts so the dashboard can show a
    # "where are we failing?" breakdown without re-parsing every case.
    # We count each individual RUN — so a flaky case contributes N rows
    # to the scorer rollup, giving a true sense of "how often does the
    # `answer_grounded` scorer fail across the whole suite?".
    scorer_rollup: dict[str, dict[str, int]] = {}
    for a in aggregates:
        for r in a.runs:
            for s in r.scores:
                b = scorer_rollup.setdefault(s.name, {"total": 0, "passed": 0})
                b["total"] += 1
                if s.passed:
                    b["passed"] += 1

    # Total runs across all cases (= sum of N per case). Useful for
    # cost/quota reasoning — at --runs 5 with 10 cases this is 50.
    total_runs = sum(a.n for a in aggregates)

    return {
        "suite": "agent",
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "total": total,
        "passed": passed,
        "failed": total - passed,
        "pass_rate": round(passed / total, 3) if total else 0.0,
        # n-of-k metadata — surfaces "you ran 50 individual chat calls
        # to produce these 10 verdicts" so callers can size cost.
        "total_runs": total_runs,
        "by_scorer": scorer_rollup,
        "cases": [a.to_dict() for a in aggregates],
    }


def cli() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(description="Run the agent eval harness.")
    ap.add_argument("--cases-dir", default=str(DEFAULT_CASES_DIR),
                    help="Directory of golden YAML cases (recursively).")
    ap.add_argument("--agent-url", default=DEFAULT_AGENT_URL)
    ap.add_argument("--auth-url", default=DEFAULT_AUTH_URL)
    ap.add_argument("--jwt-file", default=DEFAULT_JWT_FILE,
                    help="Path to file storing/caching the JWT. Created if missing.")
    ap.add_argument("--runs-dir", default=str(DEFAULT_RUNS_DIR))
    ap.add_argument("--tag", action="append", default=[],
                    help="Run only cases with this tag. Repeatable.")
    ap.add_argument("--case", action="append", default=[],
                    help="Run only cases with this id. Repeatable.")
    ap.add_argument("--concurrency", type=int, default=1)
    ap.add_argument("--with-baseline", action="store_true",
                    help="Also call the bare LLM for each case (enables the "
                         "`lift` scorer to compare agent vs raw model). "
                         "Cases with `expects_agent_lift: true` always enable this.")
    ap.add_argument("--runs", type=int, default=1, metavar="N",
                    help="Run each case N times (n-of-k flakiness tolerance). "
                         "Default 1 (back-compat with prior single-run flow). "
                         "Set 5+ for any LLM-in-loop verdict at T>0.")
    ap.add_argument("--pass-threshold", type=float, default=None, metavar="P",
                    help="Global pass-rate threshold (0.0–1.0) overriding per-case "
                         "`expected.min_pass_rate`. Default: honour per-case (or 1.0 strict).")
    args = ap.parse_args()

    cases = load_cases(args.cases_dir)
    if args.tag:
        cases = [c for c in cases if any(t in c.tags for t in args.tag)]
    if args.case:
        cases = [c for c in cases if c.id in args.case]
    if not cases:
        log.error("no cases match the filters")
        return 2

    jwt = _ensure_jwt(args.jwt_file, args.auth_url)
    log.info("running %d case(s) × %d run(s) = %d total chat call(s) against %s …",
             len(cases), args.runs, len(cases) * args.runs, args.agent_url)

    aggregates = asyncio.run(run_all(
        cases, args.agent_url, jwt, args.concurrency,
        with_baseline=args.with_baseline,
        runs_per_case=args.runs,
        cli_pass_threshold=args.pass_threshold,
    ))
    summary = _summarize(aggregates)

    # Persist artifacts: summary.json + report.md + dashboard-friendly latest.json
    runs_root = Path(args.runs_dir)
    runs_dir = runs_root / time.strftime("%Y-%m-%dT%H-%M-%S")
    runs_dir.mkdir(parents=True, exist_ok=True)
    (runs_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    # Lazy import to avoid pulling rich for non-CLI users.
    from eval.report import render_markdown
    (runs_dir / "report.md").write_text(render_markdown(aggregates))

    # Persist FULL per-case trajectories (untruncated final_answer +
    # baseline_answer + full tool inputs/outputs) for post-hoc debugging.
    # The summary.json/report.md views truncate aggressively for readability;
    # this file is the source of truth when you need to know exactly what
    # the agent said. JSONL one-row-per-RUN (n-of-k) so you can
    #   jq -c '. | select(.case_id=="x") | select(.run_idx==2)'
    # to find a specific seed in a flaky case.
    with (runs_dir / "trajectories.jsonl").open("w") as fp:
        for agg in aggregates:
            for run_idx, r in enumerate(agg.runs):
                t = r.trajectory
                fp.write(json.dumps({
                    "case_id": r.case.id,
                    "run_idx": run_idx,
                    "runs_per_case": agg.n,
                    "case_passed_aggregate": agg.passed,
                    "case_pass_rate": agg.pass_rate,
                    "run_passed": r.passed,
                    "prompt": t.prompt,
                    "final_answer": t.final_answer,
                    "baseline_answer": t.baseline_answer,
                    "baseline_error": t.baseline_error,
                    "tool_calls": [
                        {"tool": tc.tool, "input": tc.input, "output": tc.output,
                         "status": tc.status, "duration_ms": tc.duration_ms}
                        for tc in t.tool_calls
                    ],
                    "latency_ms": t.latency_ms,
                    "prompt_tokens": t.prompt_tokens,
                    "completion_tokens": t.completion_tokens,
                    "http_status": t.http_status,
                    "error": t.error,
                }) + "\n")

    # Update `runs/latest.json` — a tiny dashboard-friendly snapshot that
    # the dashboard's /api/eval endpoint reads. We write a COPY (not a
    # symlink) so the file is robust to relative-path differences.
    latest = {
        **{k: v for k, v in summary.items() if k != "cases"},
        "run_dir": str(runs_dir),
        "cases": [
            {
                "case_id": c["case_id"],
                "passed": c["passed"],
                # n-of-k context for the dashboard so it can show
                # "3/5 — flaky-tolerant" rather than just "PASS/FAIL".
                "pass_rate": c.get("pass_rate"),
                "runs": c.get("runs"),
                "threshold": c.get("threshold"),
                "latency_ms": c["latency_ms"],
                "tool_calls": c["tool_calls"],
                "scores": [
                    {"name": s["name"], "passed": s["passed"], "detail": s["detail"][:200]}
                    for s in c["scores"]
                ],
            }
            for c in summary["cases"]
        ],
    }
    runs_root.mkdir(parents=True, exist_ok=True)
    (runs_root / "latest.json").write_text(json.dumps(latest, indent=2))

    # Per-case verdict line. At --runs 1 reads like the old single-line
    # output; at --runs N it shows the n-of-k pass rate explicitly so a
    # "PASS via 3/5" verdict is unmistakable.
    for agg in aggregates:
        verdict = "✅" if agg.passed else "❌"
        if agg.n > 1:
            log.info("  %s %-40s %d/%d runs passed (threshold %.0f%%)",
                     verdict, agg.case.id, agg.passed_count, agg.n,
                     agg.effective_threshold * 100)
        else:
            log.info("  %s %s", verdict, agg.case.id)

    log.info("\nresults: %d/%d cases passed (%.0f%%) — %d total run(s) → %s",
             summary["passed"], summary["total"],
             summary["pass_rate"] * 100, summary["total_runs"], runs_dir)
    return 0 if summary["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(cli())
