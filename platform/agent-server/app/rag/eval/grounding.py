"""Deterministic citation and grounding checks for Customer QA answers."""
from __future__ import annotations

import re


SOURCE_PATTERN = re.compile(r"(?:Source|来源):\s*`?([^`\n]+)", re.I)


def extract_citations(answer: str) -> list[str]:
    return [match.strip() for match in SOURCE_PATTERN.findall(answer or "")]


def evaluate_grounding(answer: str, retrieved_sources: list[str], *, answerable: bool) -> dict:
    citations = extract_citations(answer)
    retrieved = set(retrieved_sources)
    cited = set(citations)
    valid = {source for source in cited if any(source in candidate or candidate in source for candidate in retrieved)}
    abstained = any(token in (answer or "").lower() for token in ("couldn't find", "cannot verify", "contact support", "don't have"))
    return {
        "citation_count": len(citations),
        "citation_coverage": round(len(valid) / max(1, len(retrieved)), 3),
        "citation_precision": round(len(valid) / max(1, len(cited)), 3),
        "grounded": bool(valid) if answerable else abstained,
        "abstained": abstained,
    }
