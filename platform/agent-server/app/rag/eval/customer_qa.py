"""Deterministic retrieval metrics for the Customer QA corpus."""
from __future__ import annotations

from pathlib import Path


def score_results(cases: list[dict], ranked_paths: dict[str, list[str]], top_k: int = 5) -> dict:
    """Score hit recall, MRR, and hard-negative exclusion at K."""
    hits = 0
    reciprocal = []
    negative_leaks = 0
    negative_at_one = 0
    target_at_one = 0
    for case in cases:
        paths = [Path(path).name for path in ranked_paths.get(case["id"], [])[:top_k]]
        expected = set(case.get("expected_sources", []))
        rank = next((idx + 1 for idx, path in enumerate(paths) if path in expected), None)
        hits += rank is not None
        target_at_one += bool(paths and paths[0] in expected)
        negative_at_one += bool(paths and paths[0] in set(case.get("hard_negative_sources", [])))
        reciprocal.append(1.0 / rank if rank else 0.0)
        if any(path in set(case.get("hard_negative_sources", [])) for path in paths):
            negative_leaks += 1
    total = max(1, len(cases))
    return {
        "cases": len(cases),
        "recall_at_k": round(hits / total, 3),
        "mrr_at_k": round(sum(reciprocal) / total, 3),
        "hard_negative_leak_rate": round(negative_leaks / total, 3),
        "hard_negative_at_one": round(negative_at_one / total, 3),
        "target_at_one": round(target_at_one / total, 3),
        "top_k": top_k,
    }
