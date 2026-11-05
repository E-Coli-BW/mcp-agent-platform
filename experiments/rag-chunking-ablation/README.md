# Local code chunking ablation

This experiment compares the repository's current AST chunker with fixed line windows on a hand-authored, source-grounded code-search set. It uses the locally installed `mxbai-embed-large` model and the existing in-memory vector + BM25 + RRF retriever. It does not call a generator or download a model.

## Run

From the repository root:

```bash
platform/agent-server/.venv/bin/python experiments/rag-chunking-ablation/run.py --overwrite
```

If a run stops after at least one completed variant, continue the same source snapshot and model with:

```bash
platform/agent-server/.venv/bin/python experiments/rag-chunking-ablation/run.py --resume
```

Render figures and the two analysis documents after all variants complete:

```bash
python3 experiments/rag-chunking-ablation/render_report.py
```

The renderer uses the system Python's installed Matplotlib and NumPy. The runner checks that the requested model already exists through `ollama list`; it never invokes `ollama pull`.

## Files

- `cases.json`: 36 manual queries with AST-resolved target symbols.
- `results.json`: source/model fingerprint, metrics, per-category results, and top-five retrievals for every variant.
- `summary.md`: comparison, methodology, charts, and limitations.
- `case-analysis.md`: queries where controlled fixed-window variants gained or lost the most against the controlled AST reference.
- `charts/`: PNG figures embedded by the reports.
