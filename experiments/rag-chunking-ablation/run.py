#!/usr/bin/env python3
"""Run a local-only code chunking ablation against the repository source tree.

This runner intentionally uses the project's existing Ollama embedding client
and hybrid in-memory retriever. It never pulls models or invokes a generator.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import platform
import statistics
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENT_SERVER = REPO_ROOT / "platform/agent-server"
sys.path.insert(0, str(AGENT_SERVER))

from app.rag.chunking.code import Chunk, chunk_file as ast_chunk_file  # noqa: E402
from app.rag.chunking.fixed_size import chunk_fixed_size  # noqa: E402
from app.rag.chunking.registry import chunk_file as registry_chunk_file  # noqa: E402
from app.rag.embedding.embedder import (  # noqa: E402
    EMBED_DIM,
    MAX_EMBED_CHARS,
    clean_for_embedding,
    embed_text,
)
from app.rag.index import retriever as retriever_module  # noqa: E402
from app.rag.index.retriever import InMemoryRetriever  # noqa: E402


OUTPUT_DIR = Path(__file__).resolve().parent
DATASET_PATH = OUTPUT_DIR / "cases.json"
RESULTS_PATH = OUTPUT_DIR / "results.json"
MODEL = "mxbai-embed-large"
CODE_EXTENSIONS = {".py", ".java", ".js", ".ts", ".tsx"}
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "target", "dist", "build"}
ROOTS = (
    "platform/agent-server",
    "platform/agent-server-java/src/main/java",
    "platform/agent-server-java/src/test/java",
    "platform/frontend/src",
)
TOP_K = 5

VARIANTS: list[dict[str, Any]] = [
    {"id": "ast_current", "label": "AST current", "kind": "ast"},
    {"id": "window_50_0", "label": "50 lines / 0 overlap", "kind": "window", "max_lines": 50, "overlap": 0},
    {"id": "window_50_5", "label": "50 lines / 5 overlap", "kind": "window", "max_lines": 50, "overlap": 5},
    {"id": "window_50_10", "label": "50 lines / 10 overlap", "kind": "window", "max_lines": 50, "overlap": 10},
    {"id": "window_25_5", "label": "25 lines / 5 overlap", "kind": "window", "max_lines": 25, "overlap": 5},
    {"id": "window_100_5", "label": "100 lines / 5 overlap", "kind": "window", "max_lines": 100, "overlap": 5},
    {"id": "controlled_ast", "label": "AST / file-only label", "kind": "ast", "comparison_track": "controlled", "name_strategy": "file"},
    {"id": "controlled_50_0", "label": "50 lines / 0 overlap / file label", "kind": "window", "comparison_track": "controlled", "name_strategy": "file", "max_lines": 50, "overlap": 0},
    {"id": "controlled_50_5", "label": "50 lines / 5 overlap / file label", "kind": "window", "comparison_track": "controlled", "name_strategy": "file", "max_lines": 50, "overlap": 5},
    {"id": "controlled_50_10", "label": "50 lines / 10 overlap / file label", "kind": "window", "comparison_track": "controlled", "name_strategy": "file", "max_lines": 50, "overlap": 10},
    {"id": "controlled_25_5", "label": "25 lines / 5 overlap / file label", "kind": "window", "comparison_track": "controlled", "name_strategy": "file", "max_lines": 25, "overlap": 5},
    {"id": "controlled_100_5", "label": "100 lines / 5 overlap / file label", "kind": "window", "comparison_track": "controlled", "name_strategy": "file", "max_lines": 100, "overlap": 5},
]


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    temporary.replace(path)


def _source_files() -> list[Path]:
    files: set[Path] = set()
    for relative_root in ROOTS:
        root = REPO_ROOT / relative_root
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in CODE_EXTENSIONS:
                continue
            if any(part in SKIP_DIRS for part in path.parts):
                continue
            files.add(path.resolve())
    return sorted(files)


def _source_manifest(files: list[Path]) -> dict[str, Any]:
    digest = hashlib.sha256()
    for path in files:
        relative = path.relative_to(REPO_ROOT).as_posix()
        content = path.read_bytes()
        digest.update(relative.encode())
        digest.update(b"\0")
        digest.update(hashlib.sha256(content).digest())
        digest.update(b"\n")
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPO_ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        commit = None
    return {
        "git_commit": commit,
        "working_tree_source_sha256": digest.hexdigest(),
        "source_file_count": len(files),
        "source_roots": list(ROOTS),
        "extensions": sorted(CODE_EXTENSIONS),
    }


def _local_model_record() -> dict[str, str]:
    try:
        output = subprocess.run(
            ["ollama", "list"], check=True, capture_output=True, text=True, timeout=10
        ).stdout
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("Cannot verify local Ollama model; refusing to start evaluation") from exc
    matching = [line.split() for line in output.splitlines() if line.startswith(MODEL + ":")]
    if not matching:
        raise RuntimeError(f"Local model {MODEL!r} is not installed; no download was attempted")
    fields = matching[0]
    return {"tag": fields[0], "digest_prefix": fields[1] if len(fields) > 1 else "unknown"}


def _make_variant_chunks(files: list[Path], variant: dict[str, Any]) -> tuple[list[Chunk], float]:
    started = time.perf_counter()
    chunks: list[Chunk] = []
    for path in files:
        if variant["kind"] == "ast":
            chunks.extend(registry_chunk_file(path))
        else:
            chunks.extend(
                chunk_fixed_size(
                    path,
                    max_lines=int(variant["max_lines"]),
                    overlap=int(variant["overlap"]),
                )
            )
    if variant.get("name_strategy") == "file":
        for chunk in chunks:
            chunk.name = Path(chunk.file_path).name
    return chunks, time.perf_counter() - started


def _embedding_input(chunk: Chunk) -> tuple[str, bool]:
    # Mirrors indexer.index_codebase: clip content, then prefix the chunk name;
    # embed_text subsequently cleans and clips the combined value.
    source = chunk.content[:MAX_EMBED_CHARS]
    if chunk.name:
        source = f"{chunk.name}: {source}"
    cleaned = clean_for_embedding(source)
    was_truncated = (
        len(chunk.content) > MAX_EMBED_CHARS or len(cleaned) > MAX_EMBED_CHARS
    )
    return cleaned[:MAX_EMBED_CHARS], was_truncated


def _resolve_cases(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    resolved: list[dict[str, Any]] = []
    cache: dict[str, list[Chunk]] = {}
    errors: list[str] = []
    for case in cases:
        path = (REPO_ROOT / case["file"]).resolve()
        if not path.is_file():
            errors.append(f"{case['id']}: missing file {case['file']}")
            continue
        key = path.as_posix()
        if key not in cache:
            cache[key] = ast_chunk_file(path)
        matches = [chunk for chunk in cache[key] if chunk.name == case["symbol"]]
        if not matches:
            errors.append(f"{case['id']}: symbol {case['symbol']!r} not found in {case['file']}")
            continue
        target = min(matches, key=lambda chunk: abs(chunk.start_line - int(case["line_hint"])))
        resolved.append(
            {
                **case,
                "target_start_line": target.start_line,
                "target_end_line": target.end_line,
                "target_chunk_type": target.chunk_type,
                "target_line_count": max(1, target.end_line - target.start_line + 1),
            }
        )
    if errors:
        raise RuntimeError("Unresolved evaluation cases:\n" + "\n".join(errors))
    if len(resolved) != len(cases):
        raise RuntimeError(f"Resolved {len(resolved)} of {len(cases)} cases")
    return resolved


def _interval_coverage(intervals: list[tuple[int, int]], start: int, end: int) -> float:
    clipped = sorted((max(start, a), min(end, b)) for a, b in intervals if b >= start and a <= end)
    covered = 0
    cursor = start - 1
    for left, right in clipped:
        left = max(left, cursor + 1)
        if right >= left:
            covered += right - left + 1
            cursor = right
    return min(1.0, covered / max(1, end - start + 1))


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * pct
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] * (upper - position) + ordered[upper] * (position - lower)


def _corpus_shape(files: list[Path], chunks: list[Chunk], embed_inputs: list[tuple[str, bool]]) -> dict[str, Any]:
    source_lines = 0
    chunk_lines_total = 0
    indexed_lines_by_file: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for path in files:
        try:
            source_lines += max(1, len(path.read_text(encoding="utf-8", errors="replace").splitlines()))
        except OSError:
            continue
    for chunk in chunks:
        key = Path(chunk.file_path).resolve().as_posix()
        indexed_lines_by_file[key].append((chunk.start_line, chunk.end_line))
        chunk_lines_total += max(1, chunk.end_line - chunk.start_line + 1)
    unique_indexed_lines = 0
    for intervals in indexed_lines_by_file.values():
        unique_indexed_lines += sum(
            max(0, right - left + 1)
            for left, right in _merge_intervals(intervals)
        )
    lengths = [len(chunk.content) for chunk in chunks]
    vector_bytes = len(chunks) * EMBED_DIM * 4
    text_bytes = sum(len(chunk.content.encode("utf-8")) for chunk in chunks)
    return {
        "chunk_count": len(chunks),
        "p50_chunk_chars": round(_percentile(lengths, 0.50), 1),
        "p95_chunk_chars": round(_percentile(lengths, 0.95), 1),
        "max_chunk_chars": max(lengths, default=0),
        "embed_input_truncated_chunks": sum(was_truncated for _, was_truncated in embed_inputs),
        "embed_input_truncation_rate": round(
            sum(was_truncated for _, was_truncated in embed_inputs) / max(1, len(embed_inputs)), 4
        ),
        "source_line_coverage": round(unique_indexed_lines / max(1, source_lines), 4),
        "duplicate_indexed_line_ratio": round(
            max(0, chunk_lines_total - unique_indexed_lines) / max(1, chunk_lines_total), 4
        ),
        "vector_bytes_float32_estimate": vector_bytes,
        "chunk_text_bytes_utf8": text_bytes,
    }


def _merge_intervals(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[list[int]] = []
    for start, end in sorted(intervals):
        if not merged or start > merged[-1][1] + 1:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    return [(start, end) for start, end in merged]


def _result_coverage(results: list[Any], case: dict[str, Any]) -> tuple[float, float, int | None]:
    target_start = int(case["target_start_line"])
    target_end = int(case["target_end_line"])
    relative_file = case["file"]
    matching = [
        result
        for result in results
        if Path(result.file_path).resolve().relative_to(REPO_ROOT).as_posix() == relative_file
    ]
    intervals = [(result.start_line, result.end_line) for result in matching[:TOP_K]]
    top5_coverage = _interval_coverage(intervals, target_start, target_end)
    first_result = results[:1]
    top1_matching = [
        result
        for result in first_result
        if Path(result.file_path).resolve().relative_to(REPO_ROOT).as_posix() == relative_file
    ]
    top1_coverage = _interval_coverage(
        [(result.start_line, result.end_line) for result in top1_matching],
        target_start,
        target_end,
    )
    first_hit_rank = None
    for rank, result in enumerate(results[:TOP_K], 1):
        if (
            Path(result.file_path).resolve().relative_to(REPO_ROOT).as_posix() == relative_file
            and result.end_line >= target_start
            and result.start_line <= target_end
        ):
            first_hit_rank = rank
            break
    return top1_coverage, top5_coverage, first_hit_rank


def _summarize_cases(case_results: list[dict[str, Any]]) -> dict[str, Any]:
    total = max(1, len(case_results))
    hits = [item for item in case_results if item["coverage_at_5"] > 0]
    full = [item for item in case_results if item["coverage_at_5"] >= 0.8]
    reciprocal = [1.0 / item["first_hit_rank"] if item["first_hit_rank"] else 0.0 for item in case_results]
    return {
        "cases": len(case_results),
        "hit_recall_at_5": round(len(hits) / total, 4),
        "span_recall_at_5_80pct": round(len(full) / total, 4),
        "mrr_at_5": round(statistics.fmean(reciprocal), 4),
        "mean_gold_span_coverage_at_1": round(
            statistics.fmean(item["coverage_at_1"] for item in case_results), 4
        ),
        "mean_gold_span_coverage_at_5": round(
            statistics.fmean(item["coverage_at_5"] for item in case_results), 4
        ),
        "file_hit_recall_at_5": round(
            sum(item["file_hit_at_5"] for item in case_results) / total, 4
        ),
        "by_category": _summarize_categories(case_results),
    }


def _summarize_categories(case_results: list[dict[str, Any]]) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in case_results:
        grouped[item["category"]].append(item)
    output = {}
    for category, items in sorted(grouped.items()):
        total = len(items)
        reciprocal = [1.0 / item["first_hit_rank"] if item["first_hit_rank"] else 0.0 for item in items]
        output[category] = {
            "cases": total,
            "hit_recall_at_5": round(sum(item["coverage_at_5"] > 0 for item in items) / total, 4),
            "span_recall_at_5_80pct": round(sum(item["coverage_at_5"] >= 0.8 for item in items) / total, 4),
            "mrr_at_5": round(statistics.fmean(reciprocal), 4),
            "mean_gold_span_coverage_at_5": round(
                statistics.fmean(item["coverage_at_5"] for item in items), 4
            ),
        }
    return output


async def _run(args: argparse.Namespace) -> None:
    payload = json.loads(DATASET_PATH.read_text())
    cases = _resolve_cases(payload["cases"])
    files = _source_files()
    source_info = _source_manifest(files)
    model_info = _local_model_record()

    print(f"Verified local model: {model_info['tag']} ({model_info['digest_prefix']})", flush=True)
    print(f"Source files: {len(files)}; resolved gold cases: {len(cases)}", flush=True)
    warmup = await embed_text("warm-up request for local embedding model", model=MODEL)
    if len(warmup) != EMBED_DIM or all(value == 0.0 for value in warmup):
        raise RuntimeError("Local embedding preflight failed; no evaluation started")

    query_vectors = {}
    query_times = {}
    for case in cases:
        query = case["query"]
        if query in query_vectors:
            continue
        started = time.perf_counter()
        vector = await embed_text(query, model=MODEL)
        query_times[query] = time.perf_counter() - started
        if len(vector) != EMBED_DIM or all(value == 0.0 for value in vector):
            raise RuntimeError(f"Query embedding failed for {case['id']}; aborting to avoid invalid scores")
        query_vectors[query] = vector

    original_embed = retriever_module.embed_text

    async def cached_query_embedding(text: str, *unused: Any, **unused_kwargs: Any) -> list[float]:
        return query_vectors[text]

    retriever_module.embed_text = cached_query_embedding
    try:
        completed: dict[str, Any] = {}
        if args.resume and RESULTS_PATH.exists():
            previous = json.loads(RESULTS_PATH.read_text())
            if previous.get("source", {}).get("working_tree_source_sha256") != source_info["working_tree_source_sha256"]:
                raise RuntimeError("Cannot resume: source snapshot differs from existing results")
            if previous.get("model", {}).get("digest_prefix") != model_info["digest_prefix"]:
                raise RuntimeError("Cannot resume: local embedding model differs from existing results")
            completed = {item["variant"]["id"]: item for item in previous.get("variants", [])}
        elif RESULTS_PATH.exists() and not args.overwrite:
            raise RuntimeError(f"{RESULTS_PATH} already exists; use --resume or --overwrite")

        results = {
            "experiment": "code-chunking-ablation-v1",
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "status": "running",
            "methodology": {
                "dataset_id": payload["dataset_id"],
                "dataset_provenance": payload["provenance"],
                "relevance_definition": payload["relevance_definition"],
                "primary_retriever": "InMemoryRetriever hybrid: vector + Okapi BM25 + RRF",
                "reranker": "disabled",
                "generator": "not used",
                "model": MODEL,
                "top_k": TOP_K,
                "recall_definition": "hit_recall_at_5 counts any top-5 chunk overlapping a gold span; span_recall_at_5_80pct requires >=80% union coverage.",
                "mrr_definition": "Reciprocal rank of the first top-5 result whose line span overlaps a gold span.",
                "embedding_truncation_chars": MAX_EMBED_CHARS,
                "comparison_tracks": {
                    "native_metadata": "Original AST symbol names and fixed-window filename:line names; reflects current per-strategy metadata but does not isolate chunk boundaries.",
                    "controlled": "AST and fixed-window chunks use the same filename-only name prefix in embeddings and BM25 to isolate chunk boundaries and window parameters.",
                },
            },
            "model": model_info,
            "source": source_info,
            "runtime": {"python": platform.python_version(), "platform": platform.platform()},
            "dataset": {
                "id": payload["dataset_id"],
                "case_count": len(cases),
                "sha256": hashlib.sha256(DATASET_PATH.read_bytes()).hexdigest(),
                "gold_span_resolution": "AST symbol chunks resolved by file, symbol name, and nearest line hint; symbol-centric labels may favor AST-style boundaries.",
            },
            "variants": list(completed.values()),
        }
        _write_json_atomic(RESULTS_PATH, results)

        for variant in VARIANTS:
            if variant["id"] in completed:
                print(f"Skipping completed variant {variant['label']}", flush=True)
                continue
            print(f"\n=== {variant['label']} ===", flush=True)
            chunks, chunking_seconds = _make_variant_chunks(files, variant)
            embed_inputs = [_embedding_input(chunk) for chunk in chunks]
            texts = [text for text, _ in embed_inputs]
            started = time.perf_counter()
            vectors = []
            zero_vectors = 0
            for offset in range(0, len(texts), 10):
                batch = texts[offset : offset + 10]
                batch_vectors = await asyncio.gather(
                    *(embed_text(text, model=MODEL) for text in batch)
                )
                zero_vectors += sum(all(value == 0.0 for value in vector) for vector in batch_vectors)
                if any(len(vector) != EMBED_DIM for vector in batch_vectors):
                    raise RuntimeError(f"Unexpected embedding dimension in {variant['id']}")
                vectors.extend(batch_vectors)
                if offset and offset % 500 == 0:
                    print(f"embedded {min(offset + len(batch), len(texts))}/{len(texts)}", flush=True)
                if offset + 10 < len(texts):
                    await asyncio.sleep(0.1)
            embedding_seconds = time.perf_counter() - started

            retriever = InMemoryRetriever()
            started = time.perf_counter()
            retriever.load_index(chunks, vectors)
            load_seconds = time.perf_counter() - started

            case_results = []
            search_times = []
            for case in cases:
                started = time.perf_counter()
                ranked = await retriever.search(case["query"], top_k=TOP_K)
                search_times.append(time.perf_counter() - started)
                coverage1, coverage5, first_hit = _result_coverage(ranked, case)
                file_hit = any(
                    Path(item.file_path).resolve().relative_to(REPO_ROOT).as_posix() == case["file"]
                    for item in ranked
                )
                case_results.append(
                    {
                        "id": case["id"],
                        "category": case["category"],
                        "query": case["query"],
                        "gold": {
                            "file": case["file"],
                            "symbol": case["symbol"],
                            "start_line": case["target_start_line"],
                            "end_line": case["target_end_line"],
                        },
                        "coverage_at_1": round(coverage1, 4),
                        "coverage_at_5": round(coverage5, 4),
                        "first_hit_rank": first_hit,
                        "file_hit_at_5": file_hit,
                        "top_results": [
                            {
                                "rank": rank,
                                "file": Path(result.file_path).resolve().relative_to(REPO_ROOT).as_posix(),
                                "name": result.name,
                                "chunk_type": result.chunk_type,
                                "start_line": result.start_line,
                                "end_line": result.end_line,
                                "score": round(float(result.score), 6),
                            }
                            for rank, result in enumerate(ranked, 1)
                        ],
                    }
                )

            shape = _corpus_shape(files, chunks, embed_inputs)
            result = {
                "variant": variant,
                "metrics": _summarize_cases(case_results),
                "corpus": {
                    **shape,
                    "file_count_with_chunks": len({chunk.file_path for chunk in chunks}),
                    "zero_vector_embeddings": zero_vectors,
                    "embedding_dimension": EMBED_DIM,
                },
                "cost": {
                    "chunking_seconds": round(chunking_seconds, 3),
                    "embedding_seconds": round(embedding_seconds, 3),
                    "index_load_seconds": round(load_seconds, 3),
                    "index_build_seconds": round(chunking_seconds + embedding_seconds + load_seconds, 3),
                    "query_embedding_seconds_p50": round(_percentile(list(query_times.values()), 0.5), 4),
                    "retrieval_compute_seconds_p50": round(_percentile(search_times, 0.5), 6),
                    "retrieval_compute_seconds_p95": round(_percentile(search_times, 0.95), 6),
                    "embedding_requests": len(texts),
                    "query_embeddings_cached_across_variants": len(query_vectors),
                },
                "cases": case_results,
            }
            completed[variant["id"]] = result
            results["variants"] = [completed[item["id"]] for item in VARIANTS if item["id"] in completed]
            _write_json_atomic(RESULTS_PATH, results)
            print(
                f"chunks={shape['chunk_count']} hit@5={result['metrics']['hit_recall_at_5']:.3f} "
                f"span-coverage@5={result['metrics']['mean_gold_span_coverage_at_5']:.3f} "
                f"build={result['cost']['index_build_seconds']:.1f}s",
                flush=True,
            )

        results["status"] = "complete"
        results["variants"] = [completed[item["id"]] for item in VARIANTS]
        results["resolved_cases"] = cases
        _write_json_atomic(RESULTS_PATH, results)
        print(f"\nResults written to {RESULTS_PATH}", flush=True)
    finally:
        retriever_module.embed_text = original_embed


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--resume", action="store_true", help="continue completed variants for the same source/model")
    parser.add_argument("--overwrite", action="store_true", help="replace an existing result file")
    parser.add_argument("--validate-only", action="store_true", help="resolve source spans and print corpus manifest without embedding")
    args = parser.parse_args()
    if args.validate_only:
        payload = json.loads(DATASET_PATH.read_text())
        cases = _resolve_cases(payload["cases"])
        files = _source_files()
        print(json.dumps({"cases": len(cases), "files": len(files), "source": _source_manifest(files)}, indent=2))
        return
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
