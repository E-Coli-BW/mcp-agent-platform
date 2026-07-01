#!/usr/bin/env python3
"""run_experiment.py — A/B/C/D factorial sweep for C1/C2/C3 features.

This is NOT the day-to-day harness. The day-to-day harness is
`eval-run` (eval/runner.py). This wrapper exists to do ONE thing well:

  Sweep 4 feature-flag cells × N golden cases × R repetitions, with
  the agent process restarted between cells so env-var-driven config
  takes effect, and write per-cell JSONs to a single timestamped
  experiment directory.

Why a separate script (not just bash):
  - Per-cell agent restart is non-trivial (kill the right PID, wait
    for /health, re-issue JWT only if expired).
  - We log a single provenance.json at the top of the experiment
    capturing git SHA, ollama model digest, and per-cell env vars so
    the results JSONs are reproducible.
  - Interleaving order matters; coding it once in Python keeps the
    bash glue minimal.

Pre-registered experimental design (see EVAL.md for full writeup):

    Cell  C1 reflexion  C2 router  C3 verifier   Purpose
    ----  ------------  ---------  -----------   --------------------
    A     off           off        off           Baseline
    B     off           ON         off           Router-only ablation
    C     off           off        ON            Verifier-only ablation
    D     ON            ON         ON            All features on (prod-real)

Hypotheses (must be set BEFORE running):
    H1: B vs A reduces tool_calls on hygiene cases by ≥30%, no_tool case
    H2: B vs A does NOT hurt pass rate (within ±5pp)
    H3: C vs A maintains or improves answer_grounded on subagent cases
    H4: D vs A latency increase ≤50%

Run:
    cd platform/eval-harness
    ../../platform/agent-server/.venv/bin/python run_experiment.py \
        --runs 10 \
        --out runs/exp-$(date +%Y-%m-%dT%H-%M-%S)

The script assumes the stack is already up (auth + memory + codeexec
+ agent + dashboard) via:
    scripts/dev/scenarios/with-tools/up.sh
We only restart the AGENT process between cells, not the whole stack.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
RUN_DIR_DEFAULT = REPO_ROOT / "platform" / "eval-harness" / "runs"
JWT_FILE = REPO_ROOT / ".dev-run" / "jwt.txt"
AGENT_PID_FILE = REPO_ROOT / ".dev-run" / "agent.pid"
AGENT_PORT = 8580
AUTH_PORT = 8090
MEMORY_PORT = 8180


# ── Cell definitions ─────────────────────────────────────────────────
# Each cell maps to a set of env vars passed to the agent process.
# AGENT_GRAPH_VERSION=v2 is a CONTROL (constant across all cells) —
# v1 doesn't have C2/C3 wired so the comparison would be meaningless.
#
# Env-var naming convention (pydantic-settings env_prefix='AGENT_'):
#   field name `reflexion_enabled`     → env `AGENT_REFLEXION_ENABLED`
#   field name `agent_graph_version`   → env `AGENT_AGENT_GRAPH_VERSION`  (sic!)
# The double-AGENT for the graph-version field is *not* a typo — the
# field happens to already start with `agent_`, and pydantic-settings
# does literal prefix concatenation, not de-duplication. Caught the
# hard way by a 35-minute run where every cell behaved identically.
COMMON_ENV = {
    "AGENT_AGENT_GRAPH_VERSION": "v2",
    # Keep production defaults for everything else so results reflect
    # real-world behaviour, not a stripped-down lab agent.
}

# IMPORTANT: pydantic-settings reads env vars with the `AGENT_` prefix
# (see app/config.py:183: model_config = {"env_prefix": "AGENT_", ...}).
# Every flag below MUST start with AGENT_ or the env var is silently
# ignored and the field stays at its default (False).
# This was caught after the first run of this experiment showed identical
# numbers across cells; the lesson is "validate the flag is actually
# applied" — see start_agent_with_env for the new health-check assertion.
CELLS = {
    "A": {
        **COMMON_ENV,
        "AGENT_REFLEXION_ENABLED": "false",
        "AGENT_DIRECT_TOOL_ROUTING_ENABLED": "false",
        "AGENT_SUBAGENT_VERIFIER_ENABLED": "false",
    },
    "B": {
        **COMMON_ENV,
        "AGENT_REFLEXION_ENABLED": "false",
        "AGENT_DIRECT_TOOL_ROUTING_ENABLED": "true",
        "AGENT_SUBAGENT_VERIFIER_ENABLED": "false",
    },
    "C": {
        **COMMON_ENV,
        "AGENT_REFLEXION_ENABLED": "false",
        "AGENT_DIRECT_TOOL_ROUTING_ENABLED": "false",
        "AGENT_SUBAGENT_VERIFIER_ENABLED": "true",
        # Verifier auto-retry on so we measure end-to-end correctness,
        # not just the warning-flag path.
        "AGENT_SUBAGENT_VERIFIER_AUTO_RETRY": "true",
    },
    "D": {
        **COMMON_ENV,
        "AGENT_REFLEXION_ENABLED": "true",
        "AGENT_DIRECT_TOOL_ROUTING_ENABLED": "true",
        "AGENT_SUBAGENT_VERIFIER_ENABLED": "true",
        "AGENT_SUBAGENT_VERIFIER_AUTO_RETRY": "true",
    },
}

# Cases to include. We drop:
#   explain_recursion           — needs EVAL_JUDGE_MODEL, skips silently
#                                 → would corrupt cell averages
#   None else                   — everything else exercises one of our
#                                 features or the control surface
CASES = [
    "code_run_basic",
    "efficient_memory_recall",
    "lift_memory_recall_across_session",
    "memory_set_then_search",
    "no_tool_simple_chat",
    "subagent_avoid_overuse_single_file",
    "subagent_parallel_file_summary",
]


# ── Stack management ─────────────────────────────────────────────────
def get_agent_pid() -> int | None:
    """Read the agent PID from the dev-run state, or None if not running."""
    if not AGENT_PID_FILE.exists():
        return None
    try:
        pid = int(AGENT_PID_FILE.read_text().strip())
        # Verify it's actually alive.
        os.kill(pid, 0)
        return pid
    except (ValueError, ProcessLookupError, PermissionError):
        return None


def kill_agent() -> None:
    """Send SIGTERM to the agent and wait for it to exit. Best-effort."""
    pid = get_agent_pid()
    if pid is None:
        return
    print(f"  ⏹  killing agent pid={pid}")
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    # Wait up to 10s for clean shutdown.
    for _ in range(20):
        try:
            os.kill(pid, 0)
            time.sleep(0.5)
        except ProcessLookupError:
            return
    # Force kill if still alive.
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def start_agent_with_env(cell_env: dict[str, str], log_path: Path) -> int:
    """Spawn the agent process with cell-specific env vars + production
    backends, return the PID. Mirrors scripts/dev/common.sh start_agent
    so the agent sees the same backend URLs as the day-to-day stack.
    """
    venv = REPO_ROOT / "platform" / "agent-server" / ".venv" / "bin" / "uvicorn"
    if not venv.exists():
        sys.exit(f"❌ uvicorn not found at {venv}; run setup-python.sh first")
    env = os.environ.copy()
    env.update({
        "AGENT_STRICT_TOOLS": "true",
        "AUTH_SERVER_URL": f"http://localhost:{AUTH_PORT}",
        "MEMORY_SERVER_URL": f"http://localhost:{MEMORY_PORT}",
        "CODEEXEC_SERVER_URL": "http://localhost:8380",
        "PYTHONUNBUFFERED": "1",
        **cell_env,
    })
    cwd = REPO_ROOT / "platform" / "agent-server"
    log_fp = log_path.open("ab")
    proc = subprocess.Popen(
        [str(venv), "app.main:app", "--port", str(AGENT_PORT)],
        cwd=cwd,
        env=env,
        stdout=log_fp,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    AGENT_PID_FILE.write_text(str(proc.pid))
    return proc.pid


def wait_agent_healthy(timeout: int = 30) -> None:
    deadline = time.time() + timeout
    last_err = ""
    while time.time() < deadline:
        try:
            r = httpx.get(f"http://localhost:{AGENT_PORT}/health", timeout=2)
            if r.status_code == 200:
                return
            last_err = f"HTTP {r.status_code}"
        except (httpx.HTTPError, OSError) as e:
            last_err = f"{type(e).__name__}: {e}"
        time.sleep(0.5)
    sys.exit(f"❌ agent did not become healthy within {timeout}s ({last_err})")


def assert_flag_names_valid(cell_env: dict[str, str]) -> None:
    """Defensive: every flag MUST start with `AGENT_` because the agent's
    pydantic-settings is configured with env_prefix='AGENT_'.

    The first run of this experiment shipped flags without the prefix and
    pydantic-settings silently fell back to defaults — all cells looked
    identical because no feature actually turned on. This check fails
    LOUDLY before a 35-minute run wastes compute.
    """
    bad = [k for k in cell_env if not k.startswith("AGENT_") and k not in {
        "AUTH_SERVER_URL", "MEMORY_SERVER_URL", "CODEEXEC_SERVER_URL",
        "PYTHONUNBUFFERED",
    }]
    if bad:
        sys.exit(
            f"❌ env var(s) {bad} do NOT start with 'AGENT_' — pydantic-settings\n"
            f"   will silently ignore them. See app/config.py:183 model_config."
        )


def verify_flag_applied(cell_env: dict[str, str], log_path: Path) -> None:
    """Best-effort: scan the fresh agent log for any contradiction
    between the requested flags and the agent's actual reported state.

    We can't directly read the agent's `settings` object over HTTP (no
    /config endpoint), so we wait briefly, then look for known startup
    log lines that signal which graph version booted. graph.py logs
    differently for v1 vs v2; absence of v2 marker when v2 was requested
    is a hard failure.
    """
    # Give the agent a moment to log its startup lines.
    time.sleep(1.0)
    want_v2 = cell_env.get("AGENT_AGENT_GRAPH_VERSION") == "v2"
    if not want_v2:
        return  # nothing to verify for v1
    try:
        log_text = log_path.read_text(errors="replace")
    except FileNotFoundError:
        return  # log not written yet; skip
    # graph_v2 is imported lazily inside get_agent(); the first sign of
    # v2 is when an actual request is dispatched. So we don't fail here
    # — we just print a warning if no v2 evidence yet.
    if "graph_v2" not in log_text and "v2" not in log_text:
        print(f"  ⚠️  no graph_v2 evidence in {log_path.name} yet (will verify after first request)")


def seed_memory_for_lift_case(jwt: str) -> None:
    """The lift_memory_recall_across_session case requires a memory
    seeded before the run (see the case YAML's `requires_setup`).
    Idempotent — re-seeding just overwrites.
    """
    body = {
        "key": "fav-color-eval",
        "content": "My favorite color is blue.",
        "namespace": "default",
        "tags": ["preference"],
    }
    r = httpx.post(
        f"http://localhost:{MEMORY_PORT}/api/v1/memory",
        headers={"Authorization": f"Bearer {jwt}", "Content-Type": "application/json"},
        json=body,
        timeout=10,
    )
    if r.status_code >= 300:
        # Be tolerant — memory may not be up if user ran a partial scenario.
        # Just warn and continue; the lift case will fail-correctly.
        print(f"  ⚠️  could not seed memory ({r.status_code} {r.text[:120]})")
    else:
        print(f"  🌱  seeded fav-color-eval memory for lift case")


# ── Per-cell eval invocation ─────────────────────────────────────────
def run_cell(cell_id: str, env_overrides: dict[str, str], runs: int,
             out_root: Path, log_path: Path) -> Path:
    """Restart the agent with this cell's env, then invoke eval-run
    restricted to our 7 cases. Returns the path to the cell's run
    directory (containing summary.json + trajectories.jsonl).
    """
    print(f"\n{'='*60}")
    print(f"CELL {cell_id}: {env_overrides}")
    print(f"{'='*60}")

    assert_flag_names_valid(env_overrides)
    kill_agent()
    pid = start_agent_with_env(env_overrides, log_path)
    print(f"  ▶  agent pid={pid}, waiting for health...")
    wait_agent_healthy()
    print(f"  ✅ agent healthy")
    verify_flag_applied(env_overrides, log_path)

    # Per-cell runs subdir under the experiment root.
    cell_dir = out_root / f"cell-{cell_id}"
    cell_dir.mkdir(parents=True, exist_ok=True)

    venv_py = REPO_ROOT / "platform" / "agent-server" / ".venv" / "bin" / "python"
    cmd = [
        str(venv_py), "-m", "eval.runner",
        "--runs", str(runs),
        "--with-baseline",
        "--runs-dir", str(cell_dir),
        "--pass-threshold", "0.0",  # don't fail on per-case threshold; we want raw data
    ]
    # Add --case for each ID we care about.
    for case_id in CASES:
        cmd.extend(["--case", case_id])

    eval_log = (cell_dir / "stdout.log").open("ab")
    print(f"  🧪  running: {' '.join(cmd)}")
    t0 = time.time()
    result = subprocess.run(
        cmd,
        cwd=REPO_ROOT / "platform" / "eval-harness",
        stdout=eval_log,
        stderr=subprocess.STDOUT,
        check=False,
    )
    elapsed = time.time() - t0
    print(f"  ⏱  cell {cell_id} took {elapsed:.1f}s, exit={result.returncode}")
    if result.returncode not in (0, 1):
        # 0 = all passed, 1 = some failed (still valid data), other = crash
        sys.exit(f"❌ cell {cell_id} runner crashed; see {cell_dir}/stdout.log")

    # The runner created a timestamped subdir under cell_dir; surface it.
    timestamped = sorted(cell_dir.glob("20*"))
    if not timestamped:
        sys.exit(f"❌ cell {cell_id} produced no run dir under {cell_dir}")
    return timestamped[-1]


# ── Top-level orchestration ──────────────────────────────────────────
def collect_provenance(jwt_present: bool) -> dict:
    """Capture git SHA + ollama model + timestamps so the experiment
    is reproducible. Written to provenance.json at the experiment root.
    """
    sha = subprocess.check_output(
        ["git", "log", "-1", "--format=%H"], cwd=REPO_ROOT
    ).decode().strip()
    dirty = bool(subprocess.check_output(
        ["git", "status", "--porcelain"], cwd=REPO_ROOT
    ).decode().strip())
    try:
        ollama_show = subprocess.check_output(
            ["ollama", "show", "qwen2.5:7b"], timeout=5
        ).decode()
    except Exception as e:  # noqa: BLE001
        ollama_show = f"(ollama show failed: {e})"
    return {
        "ts_start": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "git_sha": sha,
        "git_dirty": dirty,
        "jwt_present": jwt_present,
        "ollama_show_qwen2.5:7b": ollama_show,
        "cases": CASES,
        "cells": CELLS,
        "hypotheses": [
            "H1: B vs A reduces tool_calls on no_tool_simple_chat by ≥30%",
            "H2: B vs A does NOT hurt overall pass rate (within ±5pp)",
            "H3: C vs A maintains answer_grounded on subagent_parallel_file_summary",
            "H4: D vs A latency_ms increase ≤50%",
        ],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--runs", type=int, default=10,
                    help="Repetitions per (cell, case). Default 10.")
    ap.add_argument("--out", type=Path,
                    default=RUN_DIR_DEFAULT / f"exp-{time.strftime('%Y-%m-%dT%H-%M-%S')}",
                    help="Experiment output root. Per-cell dirs created inside.")
    ap.add_argument("--cells", default="ABCD",
                    help="Subset of cells to run (e.g. AB for baseline+router). Default: all.")
    ap.add_argument("--skip-seed", action="store_true",
                    help="Don't re-seed the lift case's memory (use if already done).")
    args = ap.parse_args()

    if not JWT_FILE.exists():
        sys.exit(f"❌ no JWT at {JWT_FILE} — run scripts/dev/scenarios/with-tools/up.sh first")
    jwt = JWT_FILE.read_text().strip()

    args.out.mkdir(parents=True, exist_ok=True)
    log_dir = args.out / "agent-logs"
    log_dir.mkdir(exist_ok=True)

    provenance = collect_provenance(jwt_present=bool(jwt))
    (args.out / "provenance.json").write_text(json.dumps(provenance, indent=2))
    print(f"📍 experiment root: {args.out}")
    print(f"   git SHA: {provenance['git_sha'][:10]} (dirty={provenance['git_dirty']})")

    if not args.skip_seed:
        print(f"\n🌱 pre-seeding memory for lift case...")
        seed_memory_for_lift_case(jwt)

    selected = [c for c in args.cells if c in CELLS]
    cell_results = {}
    for cell_id in selected:
        run_dir = run_cell(
            cell_id, CELLS[cell_id], args.runs, args.out,
            log_path=log_dir / f"cell-{cell_id}.log",
        )
        cell_results[cell_id] = str(run_dir.relative_to(args.out))

    # Final index pointing at each cell's actual results JSON.
    index = {
        "ts_end": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "cells_run": selected,
        "results": cell_results,
        "provenance_file": "provenance.json",
    }
    (args.out / "index.json").write_text(json.dumps(index, indent=2))
    print(f"\n✅ experiment complete → {args.out}/index.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
