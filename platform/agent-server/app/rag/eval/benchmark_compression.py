"""CLI benchmark comparing AST-aware and head-tail compression."""

from __future__ import annotations

import time

from app.rag.compression.ast import compress_code_output
from app.rag.eval.compression_cases import (
    JAVA_SAMPLE,
    LARGE_PYTHON_SAMPLE,
    NON_CODE_SAMPLE,
    PYTHON_SAMPLE,
)
from app.rag.eval.compression_metrics import (
    head_tail_compress,
    score_semantic_preservation,
)


def run_benchmark() -> None:
    samples = [
        ("Python jwt_handler.py", PYTHON_SAMPLE, "file_read"),
        ("Python order_service.py", LARGE_PYTHON_SAMPLE, "file_read"),
        ("Java UserService.java", JAVA_SAMPLE, "file_read"),
        ("Non-code file tree", NON_CODE_SAMPLE, "file_list"),
    ]
    print("=" * 80)
    print("Context Compression Benchmark: AST-aware vs Head+Tail")
    print("=" * 80)
    for name, content, tool_name in samples:
        head_started = time.perf_counter()
        head_result = head_tail_compress(content)
        head_latency = (time.perf_counter() - head_started) * 1000

        ast_started = time.perf_counter()
        ast_result = compress_code_output(content, tool_name=tool_name)
        ast_latency = (time.perf_counter() - ast_started) * 1000

        head_scores = score_semantic_preservation(content, head_result)
        ast_scores = score_semantic_preservation(content, ast_result)
        print(f"\n{name} ({len(content)} chars)")
        print(
            f"  Head+Tail: {len(head_result)} chars, "
            f"semantic={head_scores['total']}%, {head_latency:.2f}ms"
        )
        print(
            f"  AST-aware: {len(ast_result)} chars, "
            f"semantic={ast_scores['total']}%, {ast_latency:.2f}ms"
        )


__all__ = [
    "head_tail_compress",
    "run_benchmark",
    "score_semantic_preservation",
]

if __name__ == "__main__":
    run_benchmark()
