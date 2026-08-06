#!/usr/bin/env python3
"""Run the Customer QA retrieval benchmark against the configured index."""
import asyncio
import argparse
import json
from pathlib import Path

from app.rag.eval.customer_qa import score_results
from app.rag.retrieval_pipeline import apply_domain_intent_prior


def normalize_case(case: dict) -> dict:
    """Accept the reviewed retrieval schema and the generated QA schema."""
    if "expected_sources" in case:
        return case
    return {
        **case,
        "expected_sources": [item["document_id"] for item in case.get("gold_sources", [])],
        "hard_negative_sources": [item["document_id"] for item in case.get("hard_negatives", [])],
        "query": case["question"],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", type=Path, default=None)
    args = parser.parse_args()
    default_path = Path(__file__).resolve().parents[2] / "eval-harness" / "golden" / "customer_qa" / "retrieval_cases.json"
    cases_path = args.cases or default_path
    raw_cases = json.loads(cases_path.read_text())
    cases = [normalize_case(case) for case in raw_cases]

    async def run():
        from app.rag.index.retriever import get_retriever_for_tenant
        retriever = await get_retriever_for_tenant("qa-eval-tenant", collection="customer-faq")
        ranked = {}
        for case in cases:
            chunks = await retriever.search(case["query"], top_k=10)
            chunks = apply_domain_intent_prior(case["query"], chunks)
            ranked[case["id"]] = [chunk.file_path for chunk in chunks]
        result = score_results(cases, ranked)
        by_intent = {}
        for intent in sorted({case.get("intent", "unknown") for case in cases}):
            subset = [case for case in cases if case.get("intent", "unknown") == intent]
            subset_ranked = {case["id"]: ranked[case["id"]] for case in subset}
            by_intent[intent] = score_results(subset, subset_ranked)
        result["by_intent"] = by_intent
        result["dataset"] = str(cases_path)
        result["dataset_quality"] = "synthetic_candidate"
        return result

    result = asyncio.run(run())
    output = Path.home() / ".mcp-local" / "customer-qa-retrieval.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
