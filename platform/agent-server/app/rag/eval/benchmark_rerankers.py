"""Benchmark: Heuristic reranker vs Cross-encoder reranker.

Runs the same 20-query benchmark with both rerankers and compares:
- Recall@5 (how often the expected file appears in top 5)
- MRR (Mean Reciprocal Rank — how high does the expected file rank)
- Latency (ms per rerank call)

Usage:
    cd platform/agent-server
    .venv/bin/python -m app.rag.eval.benchmark_rerankers
"""

import asyncio
import json
import time
from pathlib import Path

import numpy as np

from app.rag.index.retriever import InMemoryRetriever, ScoredChunk
from app.rag.reranking.dispatcher import rerank as heuristic_rerank
from app.rag.reranking.cross_encoder import cross_encoder_rerank
from app.rag.eval.dataset import BENCHMARK


def _evaluate_with_reranker(retriever, rerank_fn, label, top_k=5):
    """Run benchmark with a specific reranker."""
    hits = 0
    reciprocal_ranks = []
    total_latency_ms = 0
    per_query = []

    loop = asyncio.get_event_loop()

    for query, expected_files in BENCHMARK:
        # Retrieve top 20 candidates (same for both rerankers)
        chunks = loop.run_until_complete(retriever.search(query, top_k=20))

        # Rerank
        t0 = time.time()
        reranked = rerank_fn(query, list(chunks), top_k=top_k)
        latency_ms = (time.time() - t0) * 1000
        total_latency_ms += latency_ms

        # Check if expected file appears in top-k
        result_files = [Path(c.file_path).name for c in reranked]
        hit = False
        best_rank = None
        for expected in expected_files:
            if expected in result_files:
                hit = True
                rank = result_files.index(expected) + 1
                if best_rank is None or rank < best_rank:
                    best_rank = rank

        if hit:
            hits += 1
            reciprocal_ranks.append(1.0 / best_rank)
        else:
            reciprocal_ranks.append(0.0)

        per_query.append({
            "query": query[:50],
            "expected": expected_files[0],
            "got": result_files[:3],
            "hit": hit,
            "rank": best_rank,
            "latency_ms": round(latency_ms, 1),
        })

    recall = hits / len(BENCHMARK)
    mrr = float(np.mean(reciprocal_ranks))
    avg_latency = total_latency_ms / len(BENCHMARK)

    return {
        "label": label,
        "recall@5": round(recall * 100, 1),
        "mrr": round(mrr, 3),
        "avg_latency_ms": round(avg_latency, 1),
        "total_latency_ms": round(total_latency_ms, 1),
        "per_query": per_query,
    }


def _evaluate_no_rerank(retriever, top_k=5):
    """Baseline: no reranking at all (raw RRF scores)."""
    hits = 0
    reciprocal_ranks = []
    loop = asyncio.get_event_loop()

    for query, expected_files in BENCHMARK:
        chunks = loop.run_until_complete(retriever.search(query, top_k=top_k))
        result_files = [Path(c.file_path).name for c in chunks]
        hit = False
        best_rank = None
        for expected in expected_files:
            if expected in result_files:
                hit = True
                rank = result_files.index(expected) + 1
                if best_rank is None or rank < best_rank:
                    best_rank = rank
        if hit:
            hits += 1
            reciprocal_ranks.append(1.0 / best_rank)
        else:
            reciprocal_ranks.append(0.0)

    return {
        "label": "No rerank (RRF only)",
        "recall@5": round(hits / len(BENCHMARK) * 100, 1),
        "mrr": round(float(np.mean(reciprocal_ranks)), 3),
        "avg_latency_ms": 0.0,
    }


def run_benchmark():
    """Run the full comparison benchmark."""
    from app.rag.index.retriever import get_retriever
    from app.rag.index.indexer import get_index_dir_for_workspace, BASE_INDEX_DIR

    retriever = get_retriever()

    # Load index
    if not retriever.is_indexed:
        platform_idx = get_index_dir_for_workspace("/Users/haosong.liu/mcp/platform")
        if not retriever.load_from_disk(str(platform_idx)):
            if not retriever.load_from_disk(str(BASE_INDEX_DIR)):
                print("❌ No index found. Run: python -m app.rag.index.indexer /path/to/project")
                return

    n_chunks = len(retriever.chunks)
    n_files = len(set(c.file_path for c in retriever.chunks))
    print(f"📊 Reranker Comparison Benchmark")
    print(f"   Index: {n_chunks} chunks from {n_files} files")
    print(f"   Queries: {len(BENCHMARK)}")
    print()

    # 1. Baseline: no reranking
    print("Running: No rerank (RRF only)...")
    baseline = _evaluate_no_rerank(retriever)

    # 2. Heuristic reranker
    print("Running: Heuristic reranker...")
    heuristic = _evaluate_with_reranker(retriever, heuristic_rerank, "Heuristic")

    # 3. Cross-encoder reranker
    print("Running: Cross-encoder reranker (first run downloads ~80MB model)...")
    cross_enc = _evaluate_with_reranker(retriever, cross_encoder_rerank, "Cross-encoder")

    # Print comparison table
    print()
    print(f"{'Reranker':<25} {'Recall@5':>10} {'MRR':>8} {'Avg Latency':>14}")
    print("=" * 60)
    for r in [baseline, heuristic, cross_enc]:
        print(f"{r['label']:<25} {r['recall@5']:>9}% {r['mrr']:>8.3f} {r['avg_latency_ms']:>12.1f}ms")
    print()

    # Improvement summary
    base_recall = baseline["recall@5"]
    heur_recall = heuristic["recall@5"]
    ce_recall = cross_enc["recall@5"]
    print("📈 Improvements over baseline (RRF only):")
    print(f"   Heuristic:     Recall {base_recall}% → {heur_recall}% ({heur_recall - base_recall:+.1f}pp)")
    print(f"   Cross-encoder: Recall {base_recall}% → {ce_recall}% ({ce_recall - base_recall:+.1f}pp)")
    print()

    base_mrr = baseline["mrr"]
    heur_mrr = heuristic["mrr"]
    ce_mrr = cross_enc["mrr"]
    print(f"   Heuristic:     MRR {base_mrr:.3f} → {heur_mrr:.3f} ({heur_mrr - base_mrr:+.3f})")
    print(f"   Cross-encoder: MRR {base_mrr:.3f} → {ce_mrr:.3f} ({ce_mrr - base_mrr:+.3f})")
    print()

    # Per-query diff (show where cross-encoder wins/loses vs heuristic)
    print("Per-query comparison (cross-encoder vs heuristic):")
    for h, c in zip(heuristic["per_query"], cross_enc["per_query"]):
        if h["hit"] != c["hit"]:
            if c["hit"] and not h["hit"]:
                print(f"  🟢 CE wins: '{c['query']}'  (CE found {c['expected']}, heuristic missed)")
            else:
                print(f"  🔴 CE loses: '{h['query']}'  (heuristic found {h['expected']}, CE missed)")
        elif h["hit"] and c["hit"] and h["rank"] != c["rank"]:
            delta = h["rank"] - c["rank"]
            if delta > 0:
                print(f"  🟢 CE better rank: '{c['query']}'  rank {h['rank']}→{c['rank']}")
            else:
                print(f"  🔴 CE worse rank: '{h['query']}'  rank {h['rank']}→{c['rank']}")

    # Save results
    results_file = Path.home() / ".mcp-local" / "reranker-benchmark.json"
    results_file.parent.mkdir(parents=True, exist_ok=True)
    all_results = {
        "baseline": baseline,
        "heuristic": {k: v for k, v in heuristic.items() if k != "per_query"},
        "cross_encoder": {k: v for k, v in cross_enc.items() if k != "per_query"},
    }
    results_file.write_text(json.dumps(all_results, indent=2))
    print(f"\n💾 Results saved to {results_file}")


if __name__ == "__main__":
    run_benchmark()
