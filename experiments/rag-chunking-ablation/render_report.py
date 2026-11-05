#!/usr/bin/env python3
"""Render charts and concise review documents from results.json."""

from __future__ import annotations

import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np


HERE = Path(__file__).resolve().parent
CHARTS = HERE / "charts"
RESULTS = HERE / "results.json"


def pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def save_metric_chart(variants: list[dict], filename: str, title: str) -> None:
    labels = [item["variant"]["label"] for item in variants]
    metrics = [
        ("mean_gold_span_coverage_at_5", "Gold-span coverage@5"),
        ("span_recall_at_5_80pct", "Queries with >=80% target@5"),
        ("mrr_at_5", "MRR@5"),
    ]
    fig, axes = plt.subplots(1, 3, figsize=(15, 5), sharey=True)
    for ax, (key, title) in zip(axes, metrics):
        values = [item["metrics"][key] for item in variants]
        bars = ax.barh(labels, values, color="#4C78A8")
        ax.set_title(title, fontsize=11)
        ax.set_xlim(0, 1.05)
        ax.set_xlabel("Score")
        ax.grid(axis="x", alpha=0.22)
        ax.set_axisbelow(True)
        for bar, value in zip(bars, values):
            ax.text(min(value + 0.02, 1.0), bar.get_y() + bar.get_height() / 2, pct(value), va="center", fontsize=8)
    axes[0].invert_yaxis()
    fig.suptitle(title, fontsize=14)
    fig.tight_layout()
    fig.savefig(CHARTS / filename, dpi=180, bbox_inches="tight")
    plt.close(fig)


def save_cost_quality_chart(variants: list[dict]) -> None:
    fig, ax = plt.subplots(figsize=(9, 6))
    label_offsets = {
        "controlled_50_0": (-120, -35),
        "controlled_50_5": (-120, -48),
        "controlled_50_10": (12, 10),
    }
    short_labels = {
        "controlled_ast": "AST / file label",
        "controlled_50_0": "50/0",
        "controlled_50_5": "50/5",
        "controlled_50_10": "50/10",
        "controlled_25_5": "25/5",
        "controlled_100_5": "100/5",
    }
    for item in variants:
        variant = item["variant"]
        x = item["corpus"]["chunk_count"]
        y = item["metrics"]["mean_gold_span_coverage_at_5"]
        truncated = item["corpus"]["embed_input_truncation_rate"]
        color = "#4C78A8" if variant["kind"] == "ast" else "#F58518"
        ax.scatter(x, y, s=90, color=color, edgecolor="white", linewidth=0.7, zorder=3)
        ax.annotate(
            f"{short_labels.get(variant['id'], variant['label'])}\ntrunc {truncated:.0%}",
            (x, y),
            xytext=label_offsets.get(variant["id"], (7, 8)),
            textcoords="offset points",
            fontsize=8,
        )
    ax.set_xlabel("Indexed chunks")
    ax.set_ylabel("Mean gold-span coverage@5")
    ax.set_ylim(0, 1.05)
    ax.set_title("Retrieval coverage vs. index size")
    ax.grid(alpha=0.22)
    fig.tight_layout()
    fig.savefig(CHARTS / "cost-quality.png", dpi=180, bbox_inches="tight")
    plt.close(fig)


def save_category_heatmap(variants: list[dict]) -> None:
    category_counts = {}
    for item in variants:
        for category, values in item["metrics"]["by_category"].items():
            category_counts[category] = values["cases"]
    categories = sorted(category_counts)
    category_labels = [f"{name} (n={category_counts[name]})" for name in categories]
    labels = [item["variant"]["label"] for item in variants]
    matrix = np.array(
        [
            [item["metrics"]["by_category"].get(category, {}).get("mean_gold_span_coverage_at_5", np.nan)
             for category in categories]
            for item in variants
        ],
        dtype=float,
    )
    fig, ax = plt.subplots(figsize=(max(9, len(categories) * 1.25), 5.5))
    image = ax.imshow(matrix, aspect="auto", vmin=0, vmax=1, cmap="YlGnBu")
    ax.set_xticks(range(len(categories)), category_labels, rotation=35, ha="right")
    ax.set_yticks(range(len(labels)), labels)
    ax.set_title("Gold-span coverage@5 by query category")
    for row in range(matrix.shape[0]):
        for col in range(matrix.shape[1]):
            if np.isfinite(matrix[row, col]):
                ax.text(col, row, pct(float(matrix[row, col])), ha="center", va="center", fontsize=8,
                        color="white" if matrix[row, col] > 0.65 else "black")
    fig.colorbar(image, ax=ax, label="Coverage")
    fig.tight_layout()
    fig.savefig(CHARTS / "category-coverage.png", dpi=180, bbox_inches="tight")
    plt.close(fig)


def save_case_heatmap(variants: list[dict]) -> None:
    cases = variants[0]["cases"]
    case_ids = [case["id"] for case in cases]
    matrix = np.array(
        [[{case["id"]: case["coverage_at_5"] for case in item["cases"]}[case_id]
          for case_id in case_ids] for item in variants],
        dtype=float,
    )
    fig, ax = plt.subplots(figsize=(15, 5.5))
    image = ax.imshow(matrix, aspect="auto", vmin=0, vmax=1, cmap="YlGnBu")
    ax.set_xticks(range(len(case_ids)), case_ids, rotation=70, ha="right", fontsize=7)
    ax.set_yticks(range(len(variants)), [item["variant"]["label"] for item in variants])
    ax.set_title("Per-query gold-span coverage@5")
    ax.set_xlabel("Evaluation question")
    fig.colorbar(image, ax=ax, label="Covered target lines")
    fig.tight_layout()
    fig.savefig(CHARTS / "per-query-coverage.png", dpi=180, bbox_inches="tight")
    plt.close(fig)


def save_chunk_structure_chart(variants: list[dict]) -> None:
    labels = [item["variant"]["label"].replace(" / file label", "") for item in variants]
    x = np.arange(len(labels))
    width = 0.36
    fig, axes = plt.subplots(1, 3, figsize=(16, 5))
    axes[0].bar(x - width / 2, [item["corpus"]["source_line_coverage"] for item in variants], width,
                label="Source-line coverage", color="#4C78A8")
    axes[0].bar(x + width / 2, [item["corpus"]["duplicate_indexed_line_ratio"] for item in variants], width,
                label="Duplicate indexed spans", color="#F58518")
    axes[0].set_title("Source coverage and overlap")
    axes[0].set_ylim(0, 1.05)
    axes[0].set_ylabel("Share")
    axes[0].legend(fontsize=8)

    axes[1].bar(x - width / 2, [item["corpus"]["p50_chunk_chars"] for item in variants], width,
                label="p50", color="#4C78A8")
    axes[1].bar(x + width / 2, [item["corpus"]["p95_chunk_chars"] for item in variants], width,
                label="p95", color="#F58518")
    axes[1].set_title("Chunk content length")
    axes[1].set_ylabel("Characters")
    axes[1].legend(fontsize=8)

    axes[2].bar(x, [item["corpus"]["embed_input_truncation_rate"] for item in variants], color="#54A24B")
    axes[2].set_title("Embedding inputs truncated")
    axes[2].set_ylabel("Share of chunks")
    axes[2].set_ylim(0, 1.05)
    for ax in axes:
        ax.set_xticks(x, labels, rotation=35, ha="right", fontsize=8)
        ax.grid(axis="y", alpha=0.2)
        ax.set_axisbelow(True)
    fig.suptitle("Index structure by controlled chunking strategy", fontsize=14)
    fig.tight_layout()
    fig.savefig(CHARTS / "chunk-structure.png", dpi=180, bbox_inches="tight")
    plt.close(fig)


def write_summary(data: dict) -> None:
    variants = [item for item in data["variants"] if item["variant"].get("comparison_track") == "controlled"]
    native_variants = [item for item in data["variants"] if item["variant"].get("comparison_track") != "controlled"]
    rows = []
    for item in variants:
        m, c, cost = item["metrics"], item["corpus"], item["cost"]
        rows.append(
            f"| {item['variant']['label']} | {c['chunk_count']} | {pct(m['file_hit_recall_at_5'])} | {pct(m['hit_recall_at_5'])} | "
            f"{pct(m['span_recall_at_5_80pct'])} | {pct(m['mean_gold_span_coverage_at_5'])} | "
            f"{m['mrr_at_5']:.3f} | {pct(c['source_line_coverage'])} | "
            f"{pct(c['duplicate_indexed_line_ratio'])} | {pct(c['embed_input_truncation_rate'])} | "
            f"{cost['index_build_seconds']:.1f}s |"
        )
    best_coverage = max(variants, key=lambda item: item["metrics"]["mean_gold_span_coverage_at_5"])
    best_mrr = max(variants, key=lambda item: item["metrics"]["mrr_at_5"])
    ast = next(item for item in variants if item["variant"]["id"] == "controlled_ast")
    by_id = {item["variant"]["id"]: item for item in variants}
    delta_cov = best_coverage["metrics"]["mean_gold_span_coverage_at_5"] - ast["metrics"]["mean_gold_span_coverage_at_5"]
    delta_mrr = best_mrr["metrics"]["mrr_at_5"] - ast["metrics"]["mrr_at_5"]
    source = data["source"]
    native_rows = []
    for item in native_variants:
        m = item["metrics"]
        native_rows.append(
            f"| {item['variant']['label']} | {item['corpus']['chunk_count']} | {pct(m['file_hit_recall_at_5'])} | {pct(m['mean_gold_span_coverage_at_5'])} | "
            f"{m['mrr_at_5']:.3f} | {pct(item['corpus']['embed_input_truncation_rate'])} |"
        )
    text = f"""# Code chunking ablation: results summary

## Result at a glance

![Controlled retrieval comparison](charts/retrieval-metrics.png)

Across {data['dataset']['case_count']} hand-authored source-grounded queries, the highest mean target-span coverage@5 was **{best_coverage['variant']['label']} ({pct(best_coverage['metrics']['mean_gold_span_coverage_at_5'])})**. The highest MRR@5 was **{best_mrr['variant']['label']} ({best_mrr['metrics']['mrr_at_5']:.3f})**. Relative to the controlled AST reference, the best observed coverage difference was {delta_cov * 100:+.1f} percentage points and the best MRR difference was {delta_mrr:+.3f}.

These are results on this local repository snapshot and this hand-authored dataset. They are not production traffic measurements or evidence of general superiority.

## Main metrics

| Controlled strategy (common file-only label) | Chunks | File hit@5 | Any target overlap@5 | >=80% target coverage@5 | Mean target coverage@5 | MRR@5 | Source lines indexed | Duplicate line spans | Embedding input truncated | Index build |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
{chr(10).join(rows)}

![Index size and retrieval quality](charts/cost-quality.png)

![Indexed coverage, duplicate line spans, chunk length, and truncation](charts/chunk-structure.png)

## Native metadata comparison

The initial end-to-end comparison retained the source's native names: AST chunks carry symbol names, while fixed windows carry filename-and-line-range names. These values are included in embeddings and BM25, so this is useful as a current-implementation comparison but does not isolate boundaries. The controlled table above normalizes that field to the filename for every strategy.

![Native metadata strategy comparison](charts/native-retrieval-metrics.png)

| Native strategy | Chunks | File hit@5 | Mean target coverage@5 | MRR@5 | Embedding input truncated |
|---|---:|---:|---:|---:|---:|
{chr(10).join(native_rows)}

## Slice and per-question behavior

![Coverage by question category](charts/category-coverage.png)

![Coverage for each query and strategy](charts/per-query-coverage.png)

See [case analysis](case-analysis.md) for concrete questions where methods disagreed. Full results, including top-five chunks and per-category values, are in [results.json](results.json).

## How to read the metrics

- **Any target overlap@5** is intentionally permissive: at least one returned chunk intersects the annotated method/class lines.
- **>=80% target coverage@5** requires the union of the top-five source ranges to cover at least 80% of the target symbol.
- **Mean target coverage@5** measures how much of the annotated symbol the top five collectively expose.
- **MRR@5** uses the rank of the first result that overlaps the target symbol by at least one line.
- **Embedding input truncated** counts a chunk when the indexer's 1500-character pre-clip removes source text or when the cleaned, name-prefixed input exceeds the embedder cap; it is not tokenizer-measured.
- **Index build** is local chunking + embedding + in-memory index loading after a model warm-up. It is hardware-specific and includes this run's sequential execution conditions.
- The hybrid retriever uses truncated embedding text for vector similarity but indexes the full chunk text for BM25. These results compare the full hybrid pipeline; they do not attribute gains to vector search alone.

## What the measured trade-offs suggest

- The controlled AST strategy covers **{pct(ast['metrics']['mean_gold_span_coverage_at_5'])}** of target lines on average and gives at least 80% coverage for **{pct(ast['metrics']['span_recall_at_5_80pct'])}** of queries. It indexes **{pct(ast['corpus']['source_line_coverage'])}** of source lines, with **{pct(ast['corpus']['duplicate_indexed_line_ratio'])}** duplicated indexed line spans from nested/overlapping syntax units.
- At 50 lines, overlap is not monotonic: 0/5/10 lines produce mean target coverage of **{pct(by_id['controlled_50_0']['metrics']['mean_gold_span_coverage_at_5'])} / {pct(by_id['controlled_50_5']['metrics']['mean_gold_span_coverage_at_5'])} / {pct(by_id['controlled_50_10']['metrics']['mean_gold_span_coverage_at_5'])}**. Five lines is the weakest of these three in this run.
- 100/5 is the strongest fixed-window option here: **{pct(by_id['controlled_100_5']['metrics']['mean_gold_span_coverage_at_5'])}** mean target coverage, **{by_id['controlled_100_5']['metrics']['mrr_at_5']:.3f} MRR@5**, and {by_id['controlled_100_5']['corpus']['chunk_count']} chunks. But **{pct(by_id['controlled_100_5']['corpus']['embed_input_truncation_rate'])}** of its embedding inputs are truncated, so its hybrid result does not prove the vector representation sees the full window.
- 25/5 nearly eliminates truncation (**{pct(by_id['controlled_25_5']['corpus']['embed_input_truncation_rate'])}**) but yields **{pct(by_id['controlled_25_5']['metrics']['mean_gold_span_coverage_at_5'])}** target coverage and creates {by_id['controlled_25_5']['corpus']['chunk_count']} chunks. Small chunks reduce truncation but can fragment the code needed to answer a query.
- AST's source-line coverage gap is a separate retrieval risk: recognized syntax nodes are indexed, so top-level imports, constants, and other code outside those nodes can be absent when a file already produced AST chunks.

Category samples are small (2–11 queries per category); slice values are diagnostic, not stable quality estimates. No confidence interval or statistical-significance claim is made.

## Scope and reproducibility

- Model: local `{data['model']['tag']}` ({data['model']['digest_prefix']}); no generator, reranker, or external model call was used.
- The embedding weight was already installed locally; no model was downloaded and no Chinese open-source model was invoked.
- Retriever: current in-memory vector + BM25 + RRF path; top-k={data['methodology']['top_k']}.
- Source snapshot: commit `{source['git_commit']}` plus working-tree source hash `{source['working_tree_source_sha256']}`.
- Indexed source files: {source['source_file_count']}; query dataset: `{data['dataset']['id']}`.
- Corpus is code-only: `{', '.join(source['extensions'])}` under the listed Python, Java, and frontend source roots; Markdown/PDF/other RAG documents were not evaluated.
- Dataset is manually authored from source code, not collected from real users. Gold spans are AST-resolved by symbol and line hint, which makes the set symbol-centric and may favor AST-style boundaries.
- The current 1500-character embedding cap and the overlapping class/method AST chunks are part of the measured baseline, not held out as separate fixes.
"""
    (HERE / "summary.md").write_text(text)


def write_case_analysis(data: dict) -> None:
    variants = [item for item in data["variants"] if item["variant"].get("comparison_track") == "controlled"]
    ast = next(item for item in variants if item["variant"]["id"] == "controlled_ast")
    ast_cases = {case["id"]: case for case in ast["cases"]}
    comparisons = []
    for alternative in variants:
        if alternative is ast:
            continue
        alt_cases = {case["id"]: case for case in alternative["cases"]}
        for case_id, ast_case in ast_cases.items():
            alt_case = alt_cases[case_id]
            comparisons.append(
                {
                    "delta": alt_case["coverage_at_5"] - ast_case["coverage_at_5"],
                    "variant": alternative["variant"]["label"],
                    "ast": ast_case,
                    "alternative": alt_case,
                }
            )
    best_per_case = {}
    worst_per_case = {}
    for item in comparisons:
        case_id = item["ast"]["id"]
        if case_id not in best_per_case or item["alternative"]["coverage_at_5"] > best_per_case[case_id]["alternative"]["coverage_at_5"]:
            best_per_case[case_id] = item
        if case_id not in worst_per_case or item["alternative"]["coverage_at_5"] < worst_per_case[case_id]["alternative"]["coverage_at_5"]:
            worst_per_case[case_id] = item
    improvements = sorted((item for item in best_per_case.values() if item["delta"] > 0), key=lambda item: item["delta"], reverse=True)[:6]
    regressions = sorted((item for item in worst_per_case.values() if item["delta"] < 0), key=lambda item: item["delta"])[:6]

    def section(title: str, entries: list[dict]) -> str:
        lines = [f"## {title}"]
        if not entries:
            return "\n".join(lines + ["\nNo cases in this direction."])
        for entry in entries:
            baseline = entry["ast"]
            alt = entry["alternative"]
            lines.extend(
                [
                    f"### {baseline['id']} · {entry['variant']} vs AST ({entry['delta'] * 100:+.1f} pp coverage)",
                    f"- Query: {baseline['query']}",
                    f"- Gold: `{baseline['gold']['file']}` · `{baseline['gold']['symbol']}` · lines {baseline['gold']['start_line']}–{baseline['gold']['end_line']}",
                    f"- AST: coverage@5 {pct(baseline['coverage_at_5'])}; first overlap rank {baseline['first_hit_rank']}; top-3: {_format_top(baseline['top_results'][:3])}",
                    f"- {entry['variant']}: coverage@5 {pct(alt['coverage_at_5'])}; first overlap rank {alt['first_hit_rank']}; top-3: {_format_top(alt['top_results'][:3])}",
                ]
            )
        return "\n".join(lines)

    text = """# Case analysis: where chunking strategies disagreed

The examples below compare target-line coverage for each query. They show retrieval outcomes, not answer-generation correctness. Chunk ranks are from the same hybrid retriever configuration.

"""
    text += section("Largest gains over controlled AST", improvements)
    text += "\n\n"
    text += section("Largest losses against controlled AST", regressions)
    (HERE / "case-analysis.md").write_text(text + "\n")


def _format_top(items: list[dict]) -> str:
    if not items:
        return "(no results)"
    return "; ".join(
        f"#{item['rank']} `{Path(item['file']).name}:{item['start_line']}-{item['end_line']}` ({item['name']})"
        for item in items
    )


def main() -> None:
    data = json.loads(RESULTS.read_text())
    if data.get("status") != "complete" or len(data.get("variants", [])) != 12:
        raise SystemExit("results.json is not complete; refusing to render final report")
    CHARTS.mkdir(exist_ok=True)
    variants = [item for item in data["variants"] if item["variant"].get("comparison_track") == "controlled"]
    native_variants = [item for item in data["variants"] if item["variant"].get("comparison_track") != "controlled"]
    save_metric_chart(variants, "retrieval-metrics.png", "Controlled comparison: common filename metadata")
    save_metric_chart(native_variants, "native-retrieval-metrics.png", "Native strategy comparison: names vary by chunker")
    save_cost_quality_chart(variants)
    save_category_heatmap(variants)
    save_case_heatmap(variants)
    save_chunk_structure_chart(variants)
    write_summary(data)
    write_case_analysis(data)
    print(f"Wrote summary, case analysis, and charts under {HERE}")


if __name__ == "__main__":
    main()
