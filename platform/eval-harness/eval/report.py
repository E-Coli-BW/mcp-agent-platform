"""Renderer: turn CaseAggregate list into a markdown report.

Markdown chosen over HTML so the report can be `cat`'d in a terminal,
linked from a PR description, and grep'd. Each case gets a header with
the verdict, then a table of scorer outcomes, then the trajectory.

Kept simple — no template engine, no rich features. The full
machine-readable data lives in summary.json; this file is for humans.

`CaseAggregate` carries N (>=1) `RunResult`s for one case. When N==1
the report reads exactly like the pre-n-of-k version. When N>1 we
surface the k/N pass count in the summary table so flaky-tolerant
cases are obvious.
"""
from __future__ import annotations

from eval.case import CaseAggregate


_VERDICT = {True: "✅ PASS", False: "❌ FAIL"}


def render_markdown(aggregates: list[CaseAggregate]) -> str:
    total = len(aggregates)
    passed = sum(1 for a in aggregates if a.passed)
    total_runs = sum(a.n for a in aggregates)
    is_nofk = any(a.n > 1 for a in aggregates)
    lines: list[str] = []
    lines.append("# Agent eval report")
    lines.append("")
    if is_nofk:
        lines.append(
            f"**{passed}/{total} cases passed** "
            f"({passed/total*100:.0f}% pass-rate, {total_runs} total runs)"
        )
    else:
        lines.append(f"**{passed}/{total} passed** ({passed/total*100:.0f}% pass-rate)")
    lines.append("")

    # Summary table — quick scan. n-of-k cases show k/N inline so a
    # "PASS via 3/5" is visually distinct from "PASS via 1/1".
    if is_nofk:
        lines.append("| case | verdict | runs | tools | tok p+c (mean) | latency (mean) |")
        lines.append("|---|---|---|---|---|---|")
    else:
        lines.append("| case | verdict | tools | tok p+c | latency |")
        lines.append("|---|---|---|---|---|")
    for a in aggregates:
        # Representative trajectory = the first run. Mean stats live in
        # the aggregate's to_dict() already; here we render directly.
        first = a.runs[0] if a.runs else None
        t = first.trajectory if first else None
        tools = ",".join(t.tool_names) if t and t.tool_names else "—"
        verdict = _VERDICT[a.passed]
        mean_latency = int(sum(r.trajectory.latency_ms for r in a.runs) / a.n) if a.n else 0
        mean_pt = int(sum(r.trajectory.prompt_tokens for r in a.runs) / a.n) if a.n else 0
        mean_ct = int(sum(r.trajectory.completion_tokens for r in a.runs) / a.n) if a.n else 0
        if is_nofk:
            run_cell = f"{a.passed_count}/{a.n} (≥{a.effective_threshold:.0%})"
            lines.append(
                f"| `{a.case.id}` | {verdict} | {run_cell} | {tools} | "
                f"{mean_pt}+{mean_ct} | {mean_latency}ms |"
            )
        else:
            lines.append(
                f"| `{a.case.id}` | {verdict} | {tools} | "
                f"{mean_pt}+{mean_ct} | {mean_latency}ms |"
            )
    lines.append("")

    # Per-case detail — only for failures by default, to keep reports short.
    # For n-of-k, "failure" means the AGGREGATE failed; we surface one
    # representative failing run (the first failing one) so the reader
    # has a concrete trajectory to look at, plus the k/N stat so they
    # know it's not the only run.
    failures = [a for a in aggregates if not a.passed]
    if failures:
        lines.append("## Failures")
        lines.append("")
        for a in failures:
            head = f"### `{a.case.id}`"
            if a.n > 1:
                head += (f" — {a.passed_count}/{a.n} runs passed "
                         f"(needed ≥{a.effective_threshold:.0%})")
            head += f" — {a.case.description or '(no description)'}"
            lines.append(head)
            lines.append("")
            lines.append(f"> {a.case.prompt}")
            lines.append("")
            # Pick a failing run for the scorer detail; if all passed
            # but aggregate failed (only possible with weird thresholds
            # like >1.0), fall back to the first run.
            target = next((r for r in a.runs if not r.passed), a.runs[0])
            lines.append("| scorer | passed | detail |")
            lines.append("|---|---|---|")
            for s in target.scores:
                lines.append(
                    f"| {s.name} | {'✅' if s.passed else '❌'} | "
                    f"{s.detail.replace('|', '\\|')} |"
                )
            lines.append("")
            if target.trajectory.tool_calls:
                lines.append("**Tool trace (representative run):**")
                lines.append("")
                for tc in target.trajectory.tool_calls:
                    lines.append(f"- `{tc.tool}` input={tc.input} → {tc.status}")
                lines.append("")
            if target.trajectory.final_answer:
                snippet = target.trajectory.final_answer[:500].replace("\n", " ")
                lines.append(f"**Final answer (representative run):** {snippet!r}")
                lines.append("")
            if target.trajectory.error:
                lines.append(f"**Transport error:** {target.trajectory.error}")
                lines.append("")
            # For n-of-k, list per-run pass/fail so the reader can see
            # which runs failed and how — flake symptom diagnosis.
            if a.n > 1:
                lines.append("**Per-run outcomes:**")
                lines.append("")
                for i, r in enumerate(a.runs):
                    flag = "✅" if r.passed else "❌"
                    failing_scorers = [s.name for s in r.scores if not s.passed]
                    detail = (
                        ", ".join(failing_scorers) if failing_scorers else "all scorers passed"
                    )
                    err = f" — error={r.trajectory.error}" if r.trajectory.error else ""
                    lines.append(f"- run {i}: {flag} ({detail}){err}")
                lines.append("")

    return "\n".join(lines)


def cli() -> int:
    """Render a previously-saved summary.json to markdown.

    Useful for re-generating a report after rule changes (no need to
    re-run the agent).
    """
    import argparse
    import json
    import sys

    ap = argparse.ArgumentParser()
    ap.add_argument("summary_json")
    ap.add_argument("-o", "--output", default=None)
    args = ap.parse_args()

    # We can't reconstruct full RunResult objects from summary.json (no
    # trajectory text), so emit a slimmed-down view.
    with open(args.summary_json) as f:
        summary = json.load(f)
    out: list[str] = []
    out.append(f"# Agent eval report (replayed)")
    out.append("")
    out.append(f"**{summary['passed']}/{summary['total']} passed**")
    out.append("")
    out.append("| case | verdict | scores |")
    out.append("|---|---|---|")
    for c in summary["cases"]:
        verdict = "✅ PASS" if c["passed"] else "❌ FAIL"
        scores = ", ".join(
            f"{s['name']}={'✅' if s['passed'] else '❌'}" for s in c["scores"]
        )
        out.append(f"| `{c['case_id']}` | {verdict} | {scores} |")

    text = "\n".join(out)
    if args.output:
        with open(args.output, "w") as f:
            f.write(text)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(cli())
