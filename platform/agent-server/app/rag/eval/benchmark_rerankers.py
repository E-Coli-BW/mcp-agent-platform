"""CLI benchmark comparing raw, heuristic, and cross-encoder ranking."""

from __future__ import annotations

import json
from pathlib import Path

from app.rag.eval.ranking import (
    BENCHMARK,
    evaluate_with_reranker,
    evaluate_without_reranker,
)
from app.rag.reranking.cross_encoder import (
    cross_encoder_available,
    cross_encoder_rerank,
)
from app.rag.reranking.dispatcher import heuristic_rerank


def _evaluate_with_reranker(retriever, rerank_fn, label, top_k=5):
    """Compatibility wrapper using the module's patchable benchmark cases."""
    return evaluate_with_reranker(
        retriever,
        rerank_fn,
        label,
        top_k=top_k,
        cases=BENCHMARK,
    )


def _evaluate_no_rerank(retriever, top_k=5):
    """Compatibility wrapper using the module's patchable benchmark cases."""
    return evaluate_without_reranker(
        retriever,
        top_k=top_k,
        cases=BENCHMARK,
    )


def run_benchmark() -> None:
    """Load the local index, run all available strategies, and save metrics."""
    from app.rag.index.indexer import BASE_INDEX_DIR, get_index_dir_for_workspace
    from app.rag.index.retriever import get_retriever

    retriever = get_retriever()
    if not retriever.is_indexed:
        platform_index = get_index_dir_for_workspace("~/mcp/platform")
        if not retriever.load_from_disk(str(platform_index)) and not retriever.load_from_disk(
            str(BASE_INDEX_DIR)
        ):
            print("No index found. Run the RAG indexer first.")
            return

    baseline = _evaluate_no_rerank(retriever)
    heuristic = _evaluate_with_reranker(
        retriever,
        heuristic_rerank,
        "Heuristic",
    )
    if cross_encoder_available():
        cross_encoder = _evaluate_with_reranker(
            retriever,
            cross_encoder_rerank,
            "Cross-encoder",
        )
    else:
        cross_encoder = {
            "label": "Cross-encoder",
            "status": "skipped",
            "reason": "sentence_transformers_unavailable",
        }

    for result in (baseline, heuristic, cross_encoder):
        print(json.dumps({key: value for key, value in result.items() if key != "per_query"}))

    results_file = Path.home() / ".mcp-local" / "reranker-benchmark.json"
    results_file.parent.mkdir(parents=True, exist_ok=True)
    results_file.write_text(
        json.dumps(
            {
                "baseline": baseline,
                "heuristic": {
                    key: value for key, value in heuristic.items() if key != "per_query"
                },
                "cross_encoder": {
                    key: value
                    for key, value in cross_encoder.items()
                    if key != "per_query"
                },
            },
            indent=2,
        )
    )
    print(f"Results saved to {results_file}")


if __name__ == "__main__":
    run_benchmark()
