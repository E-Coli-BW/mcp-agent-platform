"""score.py — post-eval aggregation + judge scoring.

PHASE 0 STATUS: stub. Loads runs/<dir>/index.jsonl and prints a basic
arm-level success rate matrix. Real judge / diff overlap scoring lands
in Phase 1.

What this WILL do (Phase 1):
  1. Read runs/<dir>/blinded/*.md + index.jsonl
  2. For each artifact, run 3 scorers:
     a. test_suite: if task.has_test, run the test against agent diff
     b. llm_judge: prompt judge with (need, ground_truth_diff, agent_output)
        → 1-5 score
     c. diff_overlap: compute Jaccard on file paths and line ranges
  3. Aggregate by arm: pass rate, mean judge score, mean overlap
  4. Output 4×3 matrix (arm × metric) as markdown + csv
  5. Spot-check sample: pick 10 artifacts, print blinded for human review

What this DOES NOW (Phase 0):
  - Load index.jsonl
  - Group by arm
  - Print pass rate (ok=true count / total) per arm
  - Print mean elapsed_ms
  - Print error breakdown if any
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from statistics import mean

HERE = Path(__file__).resolve().parent
RUNS_DIR = HERE / "runs"


def load_index(out_dir: Path) -> list[dict]:
    idx = out_dir / "index.jsonl"
    if not idx.exists():
        print(f"❌ No index.jsonl in {out_dir}. Run run.py first.", file=sys.stderr)
        sys.exit(1)
    rows = []
    with idx.open() as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def aggregate(rows: list[dict]) -> dict:
    """Group by arm; compute basic stats."""
    by_arm: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_arm[r["arm_id"]].append(r)

    out = {}
    for arm_id, runs in by_arm.items():
        total = len(runs)
        ok_count = sum(1 for r in runs if r.get("ok"))
        latencies = [r["elapsed_ms"] for r in runs if r.get("elapsed_ms") is not None]
        errors: dict[str, int] = defaultdict(int)
        for r in runs:
            if not r.get("ok"):
                key = r.get("error") or f"http_{r.get('status', 'unknown')}"
                # Truncate exception messages to bucket
                bucket = key.split(":")[0] if ":" in key else key
                errors[bucket] += 1

        out[arm_id] = {
            "total": total,
            "ok": ok_count,
            "pass_rate": ok_count / total if total else 0.0,
            "mean_latency_ms": int(mean(latencies)) if latencies else 0,
            "p95_latency_ms": int(sorted(latencies)[int(len(latencies) * 0.95)]) if len(latencies) >= 20 else None,
            "errors": dict(errors),
        }
    return out


def print_report(stats: dict) -> None:
    print("\n" + "=" * 64)
    print("📊 Phase 0 Aggregation (transport-level only — no judge yet)")
    print("=" * 64)
    print(f"\n{'Arm':<10} {'Total':>6} {'OK':>6} {'Pass %':>8} {'MeanMs':>8} {'P95Ms':>8}")
    print("-" * 50)
    for arm_id, s in sorted(stats.items()):
        p95 = str(s["p95_latency_ms"]) if s["p95_latency_ms"] is not None else "n/a"
        print(f"{arm_id:<10} {s['total']:>6} {s['ok']:>6} {s['pass_rate'] * 100:>7.1f}% {s['mean_latency_ms']:>8} {p95:>8}")

    # Error breakdown
    any_errors = any(s["errors"] for s in stats.values())
    if any_errors:
        print(f"\n{'Arm':<10} Error buckets")
        print("-" * 50)
        for arm_id, s in sorted(stats.items()):
            if s["errors"]:
                err_str = ", ".join(f"{k}={v}" for k, v in s["errors"].items())
                print(f"{arm_id:<10} {err_str}")

    print("\n⚠️  Phase 0 only checks transport (HTTP 2xx + no exception).")
    print("    NO correctness scoring yet — Phase 1 adds test_suite + judge + overlap.")


def main():
    parser = argparse.ArgumentParser(description="Aggregate runs/<dir>/index.jsonl (Phase 0 stub)")
    parser.add_argument("--runs-dir", default="smoke", help="Subdir under runs/ to read (default: smoke)")
    args = parser.parse_args()

    out_dir = RUNS_DIR / args.runs_dir
    if not out_dir.exists():
        print(f"❌ {out_dir} does not exist", file=sys.stderr)
        sys.exit(1)

    rows = load_index(out_dir)
    print(f"📂 Loaded {len(rows)} runs from {out_dir.name}/index.jsonl")
    stats = aggregate(rows)
    print_report(stats)


if __name__ == "__main__":
    main()
