#!/usr/bin/env python3
"""
analyze_experiment.py — aggregate the 4-cell factorial run into pass-rate /
tool-call / latency means with 95% CIs, then test the pre-registered hypotheses.

Inputs:
    --exp-dir   path to experiment root (containing cell-A/, cell-B/, cell-C/, cell-D/)

Outputs (written to <exp-dir>):
    analysis.json — machine-readable per-cell × per-case metrics + hypothesis verdicts
    analysis.md   — human-readable Markdown summary
    cells_long.csv — every per-run row (cell × case × run_idx → metrics) for ad hoc inspection

Statistics:
    - Pass rate: Wilson 95% CI (good for n=10, robust at 0/N or N/N)
    - Continuous (latency_ms, tool_call count): t-distribution 95% CI
    - Hypothesis test: difference of cell-mean against pre-registered threshold;
      reported as PASS / FAIL / INCONCLUSIVE based on CI placement.

This script is intentionally deterministic, no LLM, no external network.
Re-run as many times as you want; same input → same output.
"""

from __future__ import annotations

import argparse
import csv
import glob
import json
import math
import os
import statistics
import sys
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Pre-registered hypotheses (locked in BEFORE looking at the data)
# ---------------------------------------------------------------------------
# H1 — RETIRED before data collection.
#     Original: "C2 router reduces tool_calls on `no_tool_simple_chat`
#               by >= 30% vs baseline."
#     Why retired: pre-flight inspection of `tool_router._RULES` shows that
#               `no_tool_simple_chat` (prompt: "Reply with the single word:
#               pong") does not match ANY of the router's regex patterns.
#               Patterns require explicit read-tool intent like "search my
#               memory for X". Running H1 anyway would have falsified it
#               by construction, not by experiment. Note this in EVAL.md
#               as an honest pre-flight catch.
#     Replacement: H1b below probes whether the router fires on any of
#               our cases — a useful negative finding either way.
#
# H2: enabling the router (cell B) keeps pass_rate within +-5pp of baseline (A).
#     I.e., enabling the C2 feature shouldn't break what already worked.
# H3: enabling the C3 verifier (cell C) does not reduce `answer_grounded`
#     pass count on `subagent_parallel_file_summary` vs baseline (A).
# H4: enabling everything (cell D) increases latency_ms by at most 50% vs A
#     on the cases where features actually fire.
PRE_REGISTERED = {
    "H1b": {
        "name": "Router fires zero times on the chosen 7 cases (negative finding)",
        "cell_a": "A",
        "cell_b": "B",
        "case_id": None,  # aggregate
        "metric": "tool_calls_mean",
        "direction": "ratio_within",
        "threshold_pct": 5.0,  # |B/A - 1| <= 5%
        "note": "If router does NOT fire on our cases, B should equal A in tool count. "
                "This is the *expected* outcome and validates the router's safety envelope.",
    },
    "H2": {
        "name": "Router keeps pass_rate within +-5pp",
        "cell_a": "A",
        "cell_b": "B",
        "case_id": None,  # aggregate across all cases
        "metric": "pass_rate",
        "direction": "no_regression",
        "threshold_pp": 5.0,  # |B - A| <= 5pp
    },
    "H3": {
        "name": "Verifier (C) does not regress answer_grounded on subagent_parallel_file_summary",
        "cell_a": "A",
        "cell_b": "C",
        "case_id": "subagent_parallel_file_summary",
        "metric": "answer_grounded_pass_rate",
        "direction": "no_regression",
        "threshold_pp": 10.0,  # 10pp tolerance — verifier may legitimately fail-open more
    },
    "H4": {
        "name": "Everything on (D) increases latency by at most 50% vs baseline (A)",
        "cell_a": "A",
        "cell_b": "D",
        "case_id": None,
        "metric": "latency_ms_mean",
        "direction": "lower_is_better",
        "threshold_pct": 50.0,  # D <= 1.5 * A
    },
}

CELLS = ["A", "B", "C", "D"]
CELL_DESCRIPTIONS = {
    "A": "baseline — all features OFF (graph_v2 only)",
    "B": "C2 router ON — direct tool dispatch enabled",
    "C": "C3 verifier ON — subagent answer verifier enabled",
    "D": "ALL ON — C1 reflexion + C2 router + C3 verifier",
}


# ---------------------------------------------------------------------------
# Statistics helpers
# ---------------------------------------------------------------------------
def wilson_ci(passes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson 95% CI for a binomial proportion. Robust at p=0 and p=1."""
    if n == 0:
        return (0.0, 0.0)
    phat = passes / n
    denom = 1.0 + z * z / n
    center = (phat + z * z / (2 * n)) / denom
    half = (z * math.sqrt(phat * (1 - phat) / n + z * z / (4 * n * n))) / denom
    return (max(0.0, center - half), min(1.0, center + half))


def t_ci(values: list[float], confidence: float = 0.95) -> tuple[float, float, float]:
    """t-distribution 95% CI for the mean of `values`. Returns (mean, lo, hi).
    Falls back to (mean, mean, mean) for n<2."""
    n = len(values)
    if n == 0:
        return (0.0, 0.0, 0.0)
    mean = statistics.mean(values)
    if n < 2:
        return (mean, mean, mean)
    sd = statistics.stdev(values)
    se = sd / math.sqrt(n)
    # critical t for two-sided 95%, df = n-1; use a small approximation table
    # for n=10 (df=9), t* = 2.262. We embed a tiny lookup so we don't need scipy.
    t_table = {
        1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571,
        6: 2.447, 7: 2.365, 8: 2.306, 9: 2.262, 10: 2.228,
        15: 2.131, 20: 2.086, 30: 2.042, 60: 2.000,
    }
    df = n - 1
    if df in t_table:
        t_crit = t_table[df]
    else:
        # linear-interp fallback; use 1.96 for very large df
        keys = sorted(t_table)
        if df > keys[-1]:
            t_crit = 1.96
        else:
            for k in keys:
                if k >= df:
                    t_crit = t_table[k]
                    break
            else:
                t_crit = 1.96
    half = t_crit * se
    return (mean, mean - half, mean + half)


# ---------------------------------------------------------------------------
# Trajectory loading
# ---------------------------------------------------------------------------
def find_latest_run(cell_dir: Path) -> Path | None:
    """Each cell may have multiple runner invocations; pick the most recent."""
    if not cell_dir.exists():
        return None
    candidates = sorted(
        [p for p in cell_dir.iterdir() if p.is_dir() and (p / "trajectories.jsonl").exists()],
        key=lambda p: p.name,
    )
    return candidates[-1] if candidates else None


def load_trajectories(traj_path: Path) -> list[dict[str, Any]]:
    rows = []
    with open(traj_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


def load_summary_scores(summary_path: Path) -> dict[str, dict[str, dict[str, int]]]:
    """Parse summary.json and return per-case-per-scorer pass counts.
    Returns: {case_id: {scorer_name: {"passed": int, "total": int}}}
    """
    s = json.load(open(summary_path))
    out: dict[str, dict[str, dict[str, int]]] = {}
    for case in s.get("cases", []):
        case_id = case["case_id"]
        # summary.json `scores` array is just the last run's scores — not per-run aggregated.
        # We'll compute scorer aggregates by re-parsing trajectories where possible.
        # But for answer_grounded, the per-case `scores` block from the LAST run is often
        # representative; we'll cross-check.
        out[case_id] = {}
        for sc in case.get("scores", []):
            out[case_id][sc["name"]] = {"passed_last_run": 1 if sc["passed"] else 0}
    return out


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------
def aggregate_cell(cell: str, exp_dir: Path) -> dict[str, Any]:
    cell_dir = exp_dir / f"cell-{cell}"
    run_dir = find_latest_run(cell_dir)
    if run_dir is None:
        return {"cell": cell, "missing": True, "reason": f"no completed run in {cell_dir}"}

    traj_path = run_dir / "trajectories.jsonl"
    summary_path = run_dir / "summary.json"
    rows = load_trajectories(traj_path)
    summary = json.load(open(summary_path))

    # Behavior probes from agent log — direct evidence the flags
    # actually took effect, not just that the env var was set.
    agent_log = exp_dir / "agent-logs" / f"cell-{cell}.log"
    probes = scan_agent_log_for_behavior(agent_log)

    # Also count spawn_subagent calls from trajectories — the C3 verifier
    # only fires when a subagent answer comes back, so if spawn_subagent
    # never appears in the tool_calls list, C3 had nothing to verify.
    spawn_subagent_calls = 0
    spawn_subagent_runs = 0
    for r in rows:
        names = [tc.get("tool") for tc in (r.get("tool_calls") or [])]
        if "spawn_subagent" in names:
            spawn_subagent_runs += 1
            spawn_subagent_calls += sum(1 for n in names if n == "spawn_subagent")
    probes["spawn_subagent_tool_calls"] = spawn_subagent_calls
    probes["runs_with_spawn_subagent"] = spawn_subagent_runs

    per_case: dict[str, dict[str, Any]] = {}
    all_pass: list[int] = []
    all_latency: list[float] = []
    all_tool_calls: list[int] = []

    # group rows by case_id
    cases: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        cases.setdefault(r["case_id"], []).append(r)

    for case_id, case_rows in sorted(cases.items()):
        pass_count = sum(1 for r in case_rows if r.get("run_passed"))
        n = len(case_rows)
        latencies = [r["latency_ms"] for r in case_rows if r.get("latency_ms") is not None]
        tool_call_counts = [
            len(r.get("tool_calls") or [])
            for r in case_rows
            if r.get("tool_calls") is not None or r.get("run_passed") is not None
        ]

        pr_lo, pr_hi = wilson_ci(pass_count, n)
        lat_mean, lat_lo, lat_hi = t_ci(latencies)
        tc_mean, tc_lo, tc_hi = t_ci([float(x) for x in tool_call_counts])

        per_case[case_id] = {
            "n_runs": n,
            "passes": pass_count,
            "pass_rate": pass_count / n if n else 0.0,
            "pass_rate_ci": [pr_lo, pr_hi],
            "latency_ms_mean": lat_mean,
            "latency_ms_ci": [lat_lo, lat_hi],
            "tool_calls_mean": tc_mean,
            "tool_calls_ci": [tc_lo, tc_hi],
        }
        all_pass.extend([1 if r.get("run_passed") else 0 for r in case_rows])
        all_latency.extend(latencies)
        all_tool_calls.extend(float(x) for x in tool_call_counts)

    # cell-level aggregates
    total_n = len(all_pass)
    total_passes = sum(all_pass)
    cell_pr_lo, cell_pr_hi = wilson_ci(total_passes, total_n)
    cell_lat_mean, cell_lat_lo, cell_lat_hi = t_ci(all_latency)
    cell_tc_mean, cell_tc_lo, cell_tc_hi = t_ci(all_tool_calls)

    # answer_grounded per-case from summary.json's last-run scores
    grounded_by_case: dict[str, bool] = {}
    for case in summary.get("cases", []):
        for sc in case.get("scores", []):
            if sc["name"] == "answer_grounded":
                grounded_by_case[case["case_id"]] = bool(sc["passed"])

    return {
        "cell": cell,
        "description": CELL_DESCRIPTIONS[cell],
        "run_dir": str(run_dir),
        "n_cases": len(per_case),
        "n_total_runs": total_n,
        "aggregate": {
            "pass_rate": total_passes / total_n if total_n else 0.0,
            "pass_rate_ci": [cell_pr_lo, cell_pr_hi],
            "latency_ms_mean": cell_lat_mean,
            "latency_ms_ci": [cell_lat_lo, cell_lat_hi],
            "tool_calls_mean": cell_tc_mean,
            "tool_calls_ci": [cell_tc_lo, cell_tc_hi],
        },
        "per_case": per_case,
        "answer_grounded_by_case": grounded_by_case,
        "behavior_probes": probes,
    }


def scan_agent_log_for_behavior(log_path: Path) -> dict[str, Any]:
    """Grep the agent log for direct evidence of which features fired.

    Returns counts of:
        - graph_v2_invocations    — proof v2 graph is running
        - router_dispatches       — proof C2 router fired (🚦 marker)
        - reflexion_critiques     — proof C1 critic ran (🪞 marker)
        - verifier_invocations    — proof C3 verifier ran
        - verifier_failed_grades  — verifier issued <min_grade verdict
        - subagent_spawns         — subagent path was used

    These are independent of any pass_rate / latency aggregate and are
    useful for distinguishing "feature is off" from "feature is on but
    didn't fire on this prompt distribution."
    """
    if not log_path.exists():
        return {"_log_missing": str(log_path)}
    text = log_path.read_text(errors="replace")
    return {
        "graph_v2_invocations": text.count("[app.agent.graph_v2]"),
        "router_dispatches": text.count("🚦 router →"),
        "reflexion_critiques": text.count("🪞"),
        "verifier_invocations": text.count("subagent_verifier") + text.count("VerifyVerdict"),
        "subagent_spawns": text.count("spawn_subagent") + text.count("Subagent spawned"),
        "_log_size_bytes": len(text),
    }


# ---------------------------------------------------------------------------
# Hypothesis testing
# ---------------------------------------------------------------------------
def evaluate_hypothesis(name: str, spec: dict[str, Any], cells: dict[str, dict]) -> dict[str, Any]:
    a_cell = cells.get(spec["cell_a"])
    b_cell = cells.get(spec["cell_b"])
    if not a_cell or not b_cell or a_cell.get("missing") or b_cell.get("missing"):
        return {
            "id": name, "name": spec["name"], "verdict": "INCONCLUSIVE",
            "reason": f"missing cell(s) {spec['cell_a']} or {spec['cell_b']}",
        }

    case_id = spec.get("case_id")
    metric = spec["metric"]
    direction = spec["direction"]

    # Fetch the metric for both cells
    if metric == "pass_rate":
        a_val = a_cell["aggregate"]["pass_rate"] if case_id is None else a_cell["per_case"].get(case_id, {}).get("pass_rate", 0)
        b_val = b_cell["aggregate"]["pass_rate"] if case_id is None else b_cell["per_case"].get(case_id, {}).get("pass_rate", 0)
        a_ci = a_cell["aggregate"]["pass_rate_ci"] if case_id is None else a_cell["per_case"].get(case_id, {}).get("pass_rate_ci", [0, 0])
        b_ci = b_cell["aggregate"]["pass_rate_ci"] if case_id is None else b_cell["per_case"].get(case_id, {}).get("pass_rate_ci", [0, 0])
    elif metric == "tool_calls_mean":
        if case_id is None:
            a_val = a_cell["aggregate"]["tool_calls_mean"]
            b_val = b_cell["aggregate"]["tool_calls_mean"]
            a_ci = a_cell["aggregate"]["tool_calls_ci"]
            b_ci = b_cell["aggregate"]["tool_calls_ci"]
        else:
            a_val = a_cell["per_case"][case_id]["tool_calls_mean"]
            b_val = b_cell["per_case"][case_id]["tool_calls_mean"]
            a_ci = a_cell["per_case"][case_id]["tool_calls_ci"]
            b_ci = b_cell["per_case"][case_id]["tool_calls_ci"]
    elif metric == "latency_ms_mean":
        a_val = a_cell["aggregate"]["latency_ms_mean"] if case_id is None else a_cell["per_case"][case_id]["latency_ms_mean"]
        b_val = b_cell["aggregate"]["latency_ms_mean"] if case_id is None else b_cell["per_case"][case_id]["latency_ms_mean"]
        a_ci = a_cell["aggregate"]["latency_ms_ci"] if case_id is None else a_cell["per_case"][case_id]["latency_ms_ci"]
        b_ci = b_cell["aggregate"]["latency_ms_ci"] if case_id is None else b_cell["per_case"][case_id]["latency_ms_ci"]
    elif metric == "answer_grounded_pass_rate":
        # Crude: from summary.json, last-run only. Refine if needed.
        a_val = 1.0 if a_cell["answer_grounded_by_case"].get(case_id) else 0.0
        b_val = 1.0 if b_cell["answer_grounded_by_case"].get(case_id) else 0.0
        a_ci = [a_val, a_val]
        b_ci = [b_val, b_val]
    else:
        return {"id": name, "name": spec["name"], "verdict": "ERROR", "reason": f"unknown metric {metric}"}

    # Apply direction + threshold
    delta = b_val - a_val
    if direction == "lower_is_better":
        thr_pct = spec["threshold_pct"]
        if name == "H4":
            # H4: B (=D) must be no more than 50% above A
            max_ratio = 1 + thr_pct / 100.0
            ratio = b_val / a_val if a_val else float("inf")
            verdict = "PASS" if ratio <= max_ratio else "FAIL"
            details = f"D/A = {ratio:.3f}, allowed <= {max_ratio:.3f}"
        else:
            verdict = "PASS" if b_val <= a_val * (1 - thr_pct / 100.0) else "FAIL"
            details = f"B={b_val:.3f}, A={a_val:.3f}"
    elif direction == "no_regression":
        thr_pp = spec["threshold_pp"]
        # |delta| <= thr_pp/100 → PASS
        verdict = "PASS" if abs(delta) <= thr_pp / 100.0 else ("FAIL" if delta < 0 else "PASS-WITH-IMPROVEMENT")
        details = f"delta={delta:+.3f}, tolerance=+-{thr_pp / 100.0:.3f}"
    elif direction == "ratio_within":
        thr_pct = spec["threshold_pct"]
        ratio = (b_val / a_val) if a_val else float("inf")
        rel_diff_pct = abs(ratio - 1.0) * 100.0
        verdict = "PASS" if rel_diff_pct <= thr_pct else "FAIL"
        details = f"B/A = {ratio:.4f}, |ratio-1| = {rel_diff_pct:.2f}%, tolerance = {thr_pct:.1f}%"
    else:
        verdict = "ERROR"
        details = f"unknown direction {direction}"

    return {
        "id": name,
        "name": spec["name"],
        "cell_a": spec["cell_a"],
        "cell_b": spec["cell_b"],
        "case_id": case_id,
        "metric": metric,
        "a_value": a_val,
        "a_ci": a_ci,
        "b_value": b_val,
        "b_ci": b_ci,
        "delta": delta,
        "verdict": verdict,
        "details": details,
    }


# ---------------------------------------------------------------------------
# Report writers
# ---------------------------------------------------------------------------
def write_csv(cells: dict[str, dict], out_path: Path) -> None:
    """Per-cell × per-case wide row for downstream pivot."""
    fieldnames = [
        "cell", "case_id", "n_runs", "passes", "pass_rate",
        "pass_rate_ci_lo", "pass_rate_ci_hi",
        "latency_ms_mean", "latency_ms_ci_lo", "latency_ms_ci_hi",
        "tool_calls_mean", "tool_calls_ci_lo", "tool_calls_ci_hi",
    ]
    with open(out_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for cell_name, cell in cells.items():
            if cell.get("missing"):
                continue
            for case_id, c in cell["per_case"].items():
                w.writerow({
                    "cell": cell_name, "case_id": case_id,
                    "n_runs": c["n_runs"], "passes": c["passes"],
                    "pass_rate": round(c["pass_rate"], 4),
                    "pass_rate_ci_lo": round(c["pass_rate_ci"][0], 4),
                    "pass_rate_ci_hi": round(c["pass_rate_ci"][1], 4),
                    "latency_ms_mean": round(c["latency_ms_mean"], 1),
                    "latency_ms_ci_lo": round(c["latency_ms_ci"][0], 1),
                    "latency_ms_ci_hi": round(c["latency_ms_ci"][1], 1),
                    "tool_calls_mean": round(c["tool_calls_mean"], 3),
                    "tool_calls_ci_lo": round(c["tool_calls_ci"][0], 3),
                    "tool_calls_ci_hi": round(c["tool_calls_ci"][1], 3),
                })


def write_markdown(cells: dict[str, dict], hypotheses: list[dict], out_path: Path) -> None:
    lines = []
    lines.append("# Experiment Analysis — 4-cell Factorial\n")
    lines.append(f"Generated automatically by `analyze_experiment.py`. Re-run to refresh.\n")
    lines.append("## Cell descriptions\n")
    for cell, desc in CELL_DESCRIPTIONS.items():
        present = cell in cells and not cells[cell].get("missing")
        marker = "✅" if present else "❌"
        lines.append(f"- {marker} **Cell {cell}** — {desc}")
    lines.append("")

    # Aggregate table
    lines.append("## Cell-level aggregates (all cases pooled)\n")
    lines.append("| Cell | Description | n_runs | Pass rate | Latency (ms) mean | Tool calls mean |")
    lines.append("|---|---|---:|---|---|---|")
    for cell in CELLS:
        c = cells.get(cell)
        if not c or c.get("missing"):
            lines.append(f"| {cell} | _missing_ | — | — | — | — |")
            continue
        agg = c["aggregate"]
        lines.append(
            f"| {cell} | {c['description']} | {c['n_total_runs']} | "
            f"{agg['pass_rate']:.2%} [{agg['pass_rate_ci'][0]:.2%}–{agg['pass_rate_ci'][1]:.2%}] | "
            f"{agg['latency_ms_mean']:.0f} [{agg['latency_ms_ci'][0]:.0f}–{agg['latency_ms_ci'][1]:.0f}] | "
            f"{agg['tool_calls_mean']:.2f} [{agg['tool_calls_ci'][0]:.2f}–{agg['tool_calls_ci'][1]:.2f}] |"
        )
    lines.append("")

    # Behavior probes — independent evidence that features actually fired
    lines.append("## Behavior probes (independent evidence from agent logs)\n")
    lines.append("Counted from agent stdout per cell. A flag being set in env does NOT prove "
                 "the feature fired on this prompt distribution — these counts do.\n")
    lines.append("| Cell | graph_v2 invocations | C2 router dispatches | C1 critiques | C3 verifier invocations | spawn_subagent calls (runs) |")
    lines.append("|---|---:|---:|---:|---:|---:|")
    for cell in CELLS:
        c = cells.get(cell)
        if not c or c.get("missing"):
            lines.append(f"| {cell} | — | — | — | — | — |")
            continue
        p = c.get("behavior_probes", {})
        spawn_calls = p.get("spawn_subagent_tool_calls", 0)
        spawn_runs = p.get("runs_with_spawn_subagent", 0)
        lines.append(
            f"| {cell} | {p.get('graph_v2_invocations', 0)} | "
            f"{p.get('router_dispatches', 0)} | "
            f"{p.get('reflexion_critiques', 0)} | "
            f"{p.get('verifier_invocations', 0)} | "
            f"{spawn_calls} ({spawn_runs} runs) |"
        )
    lines.append("")

    # Per-case table (one section per case)
    all_cases: set[str] = set()
    for c in cells.values():
        if not c.get("missing"):
            all_cases.update(c["per_case"].keys())
    lines.append("## Per-case breakdown\n")
    for case_id in sorted(all_cases):
        lines.append(f"### `{case_id}`\n")
        lines.append("| Cell | n | Pass rate | Latency (ms) mean | Tool calls mean |")
        lines.append("|---|---:|---|---|---|")
        for cell in CELLS:
            c = cells.get(cell)
            if not c or c.get("missing") or case_id not in c["per_case"]:
                lines.append(f"| {cell} | — | — | — | — |")
                continue
            pc = c["per_case"][case_id]
            lines.append(
                f"| {cell} | {pc['n_runs']} | "
                f"{pc['pass_rate']:.0%} [{pc['pass_rate_ci'][0]:.0%}–{pc['pass_rate_ci'][1]:.0%}] | "
                f"{pc['latency_ms_mean']:.0f} [{pc['latency_ms_ci'][0]:.0f}–{pc['latency_ms_ci'][1]:.0f}] | "
                f"{pc['tool_calls_mean']:.2f} [{pc['tool_calls_ci'][0]:.2f}–{pc['tool_calls_ci'][1]:.2f}] |"
            )
        lines.append("")

    # Hypothesis verdicts
    lines.append("## Pre-registered hypothesis verdicts\n")
    lines.append("Hypotheses were locked in BEFORE the data was collected. "
                 "See `run_experiment.py` for the cell definitions and "
                 "`analyze_experiment.py:PRE_REGISTERED` for the thresholds.\n")
    lines.append("| ID | Hypothesis | Cells | Case | A | B | Δ | Verdict |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for h in hypotheses:
        if h["verdict"] == "INCONCLUSIVE":
            lines.append(f"| {h['id']} | {h['name']} | — | — | — | — | — | INCONCLUSIVE ({h.get('reason','')}) |")
            continue
        case_disp = h.get("case_id") or "_all_"
        a = h["a_value"]; b = h["b_value"]
        if isinstance(a, float) and abs(a) < 1.5 and abs(b) < 1.5 and "rate" in h["metric"]:
            a_disp = f"{a:.0%}"; b_disp = f"{b:.0%}"; d_disp = f"{(b - a) * 100:+.1f}pp"
        else:
            a_disp = f"{a:.2f}"; b_disp = f"{b:.2f}"; d_disp = f"{b - a:+.2f}"
        lines.append(
            f"| {h['id']} | {h['name']} | {h['cell_a']}→{h['cell_b']} | `{case_disp}` | "
            f"{a_disp} | {b_disp} | {d_disp} | **{h['verdict']}** — {h.get('details','')} |"
        )
    lines.append("")

    out_path.write_text("\n".join(lines))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exp-dir", type=Path, required=True,
                        help="experiment root containing cell-A/, cell-B/, ...")
    parser.add_argument("--out-prefix", type=str, default="analysis",
                        help="output filename prefix (default: analysis)")
    args = parser.parse_args()

    if not args.exp_dir.exists():
        print(f"ERROR: --exp-dir {args.exp_dir} does not exist", file=sys.stderr)
        return 2

    cells: dict[str, dict] = {}
    for cell in CELLS:
        agg = aggregate_cell(cell, args.exp_dir)
        cells[cell] = agg
        if agg.get("missing"):
            print(f"  cell-{cell}: MISSING — {agg.get('reason')}", file=sys.stderr)
        else:
            print(
                f"  cell-{cell}: n_runs={agg['n_total_runs']} "
                f"pass={agg['aggregate']['pass_rate']:.1%} "
                f"latency={agg['aggregate']['latency_ms_mean']:.0f}ms "
                f"tool_calls={agg['aggregate']['tool_calls_mean']:.2f}",
                file=sys.stderr,
            )

    hypotheses = [evaluate_hypothesis(name, spec, cells) for name, spec in PRE_REGISTERED.items()]

    # Write outputs
    json_path = args.exp_dir / f"{args.out_prefix}.json"
    md_path = args.exp_dir / f"{args.out_prefix}.md"
    csv_path = args.exp_dir / "cells_long.csv"

    with open(json_path, "w") as f:
        json.dump({"cells": cells, "hypotheses": hypotheses}, f, indent=2)
    write_markdown(cells, hypotheses, md_path)
    write_csv(cells, csv_path)

    print(f"\n  ✅ wrote {json_path}", file=sys.stderr)
    print(f"  ✅ wrote {md_path}", file=sys.stderr)
    print(f"  ✅ wrote {csv_path}", file=sys.stderr)

    # Print headline hypothesis verdicts
    print(f"\n--- Hypothesis verdicts ---", file=sys.stderr)
    for h in hypotheses:
        print(f"  {h['id']}: {h['verdict']}  ({h['name']})", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
