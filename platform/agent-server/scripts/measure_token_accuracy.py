"""Measure accuracy of our 4-chars/token heuristic vs actual tokenization.

Compares len(text)//4 against tiktoken (GPT tokenizer) on real tool outputs.
Reports: mean error, P95 error, worst case.

Usage:
    cd platform/agent-server
    .venv/bin/python scripts/measure_token_accuracy.py
"""

import json
from pathlib import Path

try:
    import tiktoken
    enc = tiktoken.encoding_for_model("gpt-4")
except ImportError:
    print("pip install tiktoken first")
    exit(1)


def measure_accuracy():
    """Compare heuristic vs tiktoken on real code chunks."""
    # Load real chunks from our RAG index
    chunks_file = Path.home() / ".mcp-local" / "rag-index" / "chunks.json"
    
    # Also try workspace-specific indexes
    if not chunks_file.exists():
        for d in (Path.home() / ".mcp-local" / "rag-index").iterdir():
            if d.is_dir() and (d / "chunks.json").exists():
                chunks_file = d / "chunks.json"
                break

    if not chunks_file.exists():
        print("❌ No chunks.json found. Run the indexer first.")
        return

    chunks = json.loads(chunks_file.read_text())
    print(f"Loaded {len(chunks)} chunks from {chunks_file.parent.name}")

    errors = []
    for chunk in chunks:
        text = chunk["content"]
        actual_tokens = len(enc.encode(text))
        estimated_tokens = len(text) // 4  # our heuristic

        if actual_tokens == 0:
            continue

        error_pct = abs(estimated_tokens - actual_tokens) / actual_tokens * 100
        errors.append({
            "name": chunk.get("name", "?"),
            "language": chunk.get("language", "?"),
            "chars": len(text),
            "actual_tokens": actual_tokens,
            "estimated_tokens": estimated_tokens,
            "error_pct": round(error_pct, 1),
            "direction": "over" if estimated_tokens > actual_tokens else "under",
        })

    if not errors:
        print("No valid chunks to analyze")
        return

    errors.sort(key=lambda e: e["error_pct"], reverse=True)
    error_pcts = [e["error_pct"] for e in errors]

    import numpy as np
    print(f"\n📊 Token Estimation Accuracy ({len(errors)} chunks)")
    print(f"   Heuristic: len(text) // 4")
    print(f"   Ground truth: tiktoken (GPT-4 tokenizer)")
    print()
    print(f"   Mean error:   {np.mean(error_pcts):.1f}%")
    print(f"   Median error: {np.median(error_pcts):.1f}%")
    print(f"   P95 error:    {np.percentile(error_pcts, 95):.1f}%")
    print(f"   Max error:    {max(error_pcts):.1f}%")
    print()

    # Direction analysis
    over = sum(1 for e in errors if e["direction"] == "over")
    under = sum(1 for e in errors if e["direction"] == "under")
    print(f"   Over-estimates:  {over} ({over/len(errors)*100:.0f}%)")
    print(f"   Under-estimates: {under} ({under/len(errors)*100:.0f}%)")
    print()

    # Per-language breakdown
    by_lang = {}
    for e in errors:
        lang = e["language"]
        if lang not in by_lang:
            by_lang[lang] = []
        by_lang[lang].append(e["error_pct"])

    print(f"   {'Language':<12} {'Mean Error':>12} {'Samples':>10}")
    print(f"   {'-'*12} {'-'*12} {'-'*10}")
    for lang, errs in sorted(by_lang.items()):
        print(f"   {lang:<12} {np.mean(errs):>11.1f}% {len(errs):>10}")

    print()
    print("   Worst 5 estimates:")
    for e in errors[:5]:
        print(f"     {e['name']:<30} {e['chars']:>6} chars → "
              f"actual={e['actual_tokens']:>4} est={e['estimated_tokens']:>4} "
              f"error={e['error_pct']}% ({e['direction']})")

    # Recommendation
    avg_ratio = np.mean([e["chars"] / e["actual_tokens"] for e in errors if e["actual_tokens"] > 0])
    print(f"\n   📐 Actual average chars/token: {avg_ratio:.2f}")
    print(f"   Current heuristic: 4.0 chars/token")
    if abs(avg_ratio - 4.0) > 0.3:
        print(f"   ⚠️  Recommendation: change to {avg_ratio:.1f} chars/token for better accuracy")
    else:
        print(f"   ✅ Current heuristic is within acceptable range")


if __name__ == "__main__":
    measure_accuracy()
