"""Console rendering and persistence for classifier evaluation."""

from __future__ import annotations

import json
from pathlib import Path

from app.agent.intent.evaluation import (
    EvalResult,
    evaluate_direct_routing_classifier,
    evaluate_intent_classifiers,
    evaluate_topic_switch_classifiers,
)


def _print_results(title: str, results: list[EvalResult]) -> None:
    print(f"\n{title}")
    print("-" * 70)
    print(f"  {'Strategy':<15} {'Accuracy':>10} {'Correct':>10} {'Latency':>12}")
    for result in results:
        print(
            f"  {result.strategy:<15} {result.accuracy:>9}% "
            f"{result.correct}/{result.total:>7} {result.latency_ms:>10.1f}ms"
        )
        for error in result.errors:
            print(
                f"    '{error['input'][:50]}' -> expected={error['expected']}, "
                f"got={error['predicted']}"
            )


async def run_full_evaluation() -> None:
    """Run classifier comparisons, print metrics, and save structured results."""
    print("=" * 70)
    print("  Heuristic vs LLM Classifier - A/B Evaluation")
    print("=" * 70)

    intent_results = await evaluate_intent_classifiers()
    topic_results = await evaluate_topic_switch_classifiers()
    routing_result = evaluate_direct_routing_classifier()
    _print_results("Intent Classification", intent_results)
    _print_results("Topic Switch Detection", topic_results)
    _print_results("Direct Tool Routing", [routing_result])

    all_results = [
        {
            "strategy": result.strategy,
            "task": result.task,
            "accuracy": result.accuracy,
            "latency_ms": result.latency_ms,
            "errors": result.errors,
        }
        for result in [*intent_results, *topic_results, routing_result]
    ]
    results_file = Path.home() / ".mcp-local" / "classifier-eval-results.json"
    results_file.parent.mkdir(parents=True, exist_ok=True)
    results_file.write_text(json.dumps(all_results, indent=2))
    print(f"\nResults saved to {results_file}")
