"""CI gate — fail the build if eval results regress vs baseline.

Two failure modes:

  1. **Floor failure**: absolute pass-rate below `--min-pass-rate`.
     Catches "everything broke".

  2. **Regression**: a case that PASSED in the baseline now FAILS.
     Catches "this PR broke a previously-working scenario", which is
     the more dangerous signal (a small refactor that silently
     regresses one case is easy to miss in a 50-case eval).

Usage in CI:

    eval-run --runs-dir runs/   # produces runs/<ts>/summary.json
    eval-gate runs/latest/summary.json --baseline baselines/master.json

Baselines are committed to the repo (small JSON, ~5KB per 50 cases).
Update via:

    cp runs/<ts>/summary.json baselines/master.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _load(path: str) -> dict:
    with open(path) as f:
        return json.load(f)


def gate(current: dict, baseline: dict | None, min_pass_rate: float) -> tuple[bool, list[str]]:
    """Return (ok, issues). ok=True means the gate passes."""
    issues: list[str] = []

    pr = current.get("pass_rate", 0.0)
    if pr < min_pass_rate:
        issues.append(f"pass_rate {pr:.2%} < min {min_pass_rate:.2%}")

    if baseline:
        baseline_pass = {c["case_id"]: c["passed"] for c in baseline["cases"]}
        for c in current["cases"]:
            cid = c["case_id"]
            if baseline_pass.get(cid) is True and not c["passed"]:
                issues.append(f"regression: {cid} was passing in baseline, now failing")

    return (not issues), issues


def cli() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("summary_json", help="Current run summary.json")
    ap.add_argument("--baseline", default=None,
                    help="Baseline summary.json to compare against (optional).")
    ap.add_argument("--min-pass-rate", type=float, default=0.90,
                    help="Floor pass-rate; default 0.90.")
    args = ap.parse_args()

    current = _load(args.summary_json)
    baseline = _load(args.baseline) if args.baseline and Path(args.baseline).exists() else None
    if args.baseline and baseline is None:
        print(f"⚠  baseline missing ({args.baseline}); only floor check applies.", file=sys.stderr)

    ok, issues = gate(current, baseline, args.min_pass_rate)
    if ok:
        print(f"✅ eval-gate PASS — pass_rate={current['pass_rate']:.2%}")
        return 0
    print("❌ eval-gate FAIL:")
    for i in issues:
        print(f"  - {i}")
    return 1


if __name__ == "__main__":
    sys.exit(cli())
