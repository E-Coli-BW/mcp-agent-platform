#!/usr/bin/env python3
"""Quality gate for Customer QA and agent-aware eval artifacts.

Usage: customer_qa_gate.py runs/smoke/latest.json --min-pass-rate 0.8
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("summary", type=Path)
    parser.add_argument("--min-pass-rate", type=float, default=0.8)
    parser.add_argument("--min-grounded-rate", type=float, default=0.9)
    parser.add_argument("--min-refusal-rate", type=float, default=0.9)
    args = parser.parse_args()
    data = json.loads(args.summary.read_text())
    failures: list[str] = []
    pass_rate = float(data.get("pass_rate", 0.0))
    if pass_rate < args.min_pass_rate:
        failures.append(f"pass_rate {pass_rate:.3f} < {args.min_pass_rate:.3f}")
    scorers = data.get("by_scorer", {})
    grounded = scorers.get("answer_grounded", {})
    grounded_rate = grounded.get("passed", 0) / grounded.get("total", 1)
    if grounded_rate < args.min_grounded_rate:
        failures.append(f"answer_grounded {grounded_rate:.3f} < {args.min_grounded_rate:.3f}")
    refusal = scorers.get("refusal_accuracy")
    if refusal:
        refusal_rate = refusal.get("passed", 0) / max(1, refusal.get("total", 0))
        if refusal_rate < args.min_refusal_rate:
            failures.append(f"refusal_accuracy {refusal_rate:.3f} < {args.min_refusal_rate:.3f}")
    result = {"passed": not failures, "agent_id": data.get("agent_id"), "run_type": data.get("run_type"), "failures": failures}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
