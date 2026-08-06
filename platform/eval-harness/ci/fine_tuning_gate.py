"""Decide whether local fine-tuning is justified by measured evidence.

This is deliberately a gate, not a trainer: it prevents a noisy local feedback
set from turning into an expensive or irreversible model change.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def decide(data: dict, *, min_samples: int = 500, max_quality: float = 0.75) -> dict:
    samples = int(data.get("feedback_samples", 0))
    quality = float(data.get("quality_rate", 0.0))
    held_out = bool(data.get("held_out_set", False))
    if samples < min_samples:
        decision = "defer_insufficient_data"
        reason = f"feedback_samples {samples} < {min_samples}"
    elif not held_out:
        decision = "defer_no_held_out_set"
        reason = "a held-out evaluation set is required before training"
    elif quality > max_quality:
        decision = "prefer_rag_or_policy"
        reason = f"quality_rate {quality:.3f} is already above {max_quality:.3f}"
    else:
        decision = "train_candidate_then_gate"
        reason = "enough labels and a held-out set exist; compare a candidate model"
    return {"decision": decision, "reason": reason, "local_first": True}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("summary", type=Path)
    parser.add_argument("--min-samples", type=int, default=500)
    args = parser.parse_args()
    result = decide(json.loads(args.summary.read_text()), min_samples=args.min_samples)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
