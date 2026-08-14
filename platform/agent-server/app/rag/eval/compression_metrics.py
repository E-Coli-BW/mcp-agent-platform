"""Compression baselines and semantic-preservation metrics."""

from __future__ import annotations

import re


def head_tail_compress(content: str, max_chars: int = 1500) -> str:
    """Apply the legacy head-and-tail baseline used for comparison."""
    if len(content) <= max_chars:
        return content
    return (
        content[:600]
        + "\n\n... (previous tool output summarized to save context) ...\n\n"
        + content[-400:]
    )


def score_semantic_preservation(original: str, compressed: str) -> dict[str, int]:
    """Measure retained structural identifiers and documentation."""
    function_names = set(re.findall(r"def (\w+)", original)) | set(
        re.findall(
            r"(?:public|private|protected).*?(\w+)\s*\(",
            original,
        )
    )
    return_statements = set(re.findall(r"return\s+\S+", original))
    class_names = set(re.findall(r"class (\w+)", original))
    imports = set(re.findall(r"(?:import|from)\s+\S+", original))
    has_docstring = bool(
        re.search(r'""".*?"""|\'\'\'.+?\'\'\'|/\*\*.*?\*/', original, re.DOTALL)
    )

    scores = {}
    _add_identifier_score(scores, "function_names", function_names, compressed)
    _add_identifier_score(scores, "return_statements", return_statements, compressed)
    _add_identifier_score(scores, "class_names", class_names, compressed)
    _add_identifier_score(scores, "imports", imports, compressed)
    scores["has_docstring"] = (
        100
        if has_docstring
        and any(marker in compressed for marker in ('"""', "'''", "/**", "Docstring"))
        else 0
    )
    scores["total"] = round(sum(scores.values()) / len(scores)) if scores else 0
    return scores


def _add_identifier_score(
    scores: dict[str, int],
    name: str,
    identifiers: set[str],
    compressed: str,
) -> None:
    if not identifiers:
        return
    preserved = sum(identifier in compressed for identifier in identifiers)
    scores[name] = round(preserved / len(identifiers) * 100)
