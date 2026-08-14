"""Stable RAG retrieval benchmark exports and CLI."""

from __future__ import annotations

import json
from pathlib import Path

from app.rag.eval.ranking import BENCHMARK, evaluate_retriever


def run_evaluation() -> None:
    """Load the local index, evaluate hybrid retrieval, and persist metrics."""
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

    results = evaluate_retriever(
        retriever,
        strategy="hybrid",
        top_k_values=(3, 5, 10),
    )
    print(f"RAG Retrieval Evaluation ({len(BENCHMARK)} queries)")
    print(json.dumps(results, indent=2))

    results_file = Path.home() / ".mcp-local" / "rag-eval-results.json"
    results_file.parent.mkdir(parents=True, exist_ok=True)
    results_file.write_text(json.dumps(results, indent=2))
    print(f"Results saved to {results_file}")


__all__ = ["BENCHMARK", "evaluate_retriever", "run_evaluation"]

if __name__ == "__main__":
    run_evaluation()
