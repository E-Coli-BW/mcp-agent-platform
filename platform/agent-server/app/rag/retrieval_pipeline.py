"""Query and context controls that sit between retrieval and generation."""
from __future__ import annotations

import re
import copy


def rewrite_query(query: str, conversation: list[str] | None = None) -> str:
    """Conservative deterministic rewrite; never invents domain facts."""
    text = " ".join((conversation or [])[-2:] + [query]).strip()
    text = re.sub(r"\s+", " ", text)
    # Keep identifiers and policy terms intact while removing conversational noise.
    text = re.sub(r"^(please|could you|can you)\s+", "", text, flags=re.I)
    return text[:1000]


DOMAIN_TERMS = {
    "returns-and-refunds.md": {"return", "refund", "damaged", "receipt"},
    "shipping-status.md": {"package", "shipping", "tracking", "track", "delivery", "where"},
    "address-changes.md": {"address", "mailing", "change", "update"},
}


def classify_customer_qa_intent(query: str) -> tuple[str, float]:
    terms = set(re.findall(r"\w+", query.lower()))
    scores = {intent: len(terms & keywords) for intent, keywords in {
        "returns_policy": {"return", "refund", "damaged", "receipt"},
        "shipping_status": {"package", "shipping", "tracking", "track", "delivery"},
        "address_change": {"address", "mailing", "change", "update"},
    }.items()}
    intent, score = max(scores.items(), key=lambda item: item[1])
    total = max(1, sum(scores.values()))
    return (intent, score / total) if score else ("unknown", 0.0)


def filter_by_intent(query: str, results: list, min_confidence: float = 0.75) -> list:
    """Apply source filtering only for high-confidence, known intents."""
    intent, confidence = classify_customer_qa_intent(query)
    if confidence < min_confidence:
        return results
    allowed = {
        "returns_policy": "returns-and-refunds.md",
        "shipping_status": "shipping-status.md",
        "address_change": "address-changes.md",
    }[intent]
    matching = [result for result in results if result.file_path.rsplit("/", 1)[-1] == allowed]
    return matching or results


def apply_domain_intent_prior(query: str, results: list, boost: float = 0.08) -> list:
    """Boost the source family matching explicit domain terms.

    This is intentionally a small, inspectable prior. It is not an ACL and it
    never removes evidence; it only resolves common Customer QA intent ties.
    """
    terms = set(re.findall(r"\w+", query.lower()))
    updated = []
    for result in results:
        filename = result.file_path.rsplit("/", 1)[-1]
        matched = DOMAIN_TERMS.get(filename, set()) & terms
        item = copy.copy(result)
        if matched:
            item.score += boost * len(matched)
        updated.append(item)
    return sorted(updated, key=lambda item: item.score, reverse=True)


def deduplicate_and_diversify(results: list, top_k: int, diversity: float = 0.35) -> list:
    """Select relevant results while avoiding repeated chunks from one source."""
    selected = []
    seen_keys = set()
    for result in sorted(results, key=lambda item: item.score, reverse=True):
        metadata = getattr(result, "metadata", None) or {}
        key = metadata.get("parent_id") or f"{result.file_path}:{result.start_line}"
        if key in seen_keys:
            continue
        # A mild source penalty approximates MMR without another embedding call.
        source_count = sum(item.file_path == result.file_path for item in selected)
        adjusted = result.score - diversity * 0.05 * source_count
        updated = copy.copy(result)
        updated.score = adjusted
        selected.append(updated)
        seen_keys.add(key)
        if len(selected) >= top_k:
            break
    return selected


def expand_parent_context(result, parent_lookup: dict[str, str] | None = None):
    """Attach parent section text when the index stores a parent reference."""
    metadata = getattr(result, "metadata", None) or {}
    parent_id = metadata.get("parent_id")
    if not parent_id or not parent_lookup or parent_id not in parent_lookup:
        return result
    updated = copy.copy(result)
    updated.content = parent_lookup[parent_id] + "\n\n" + result.content
    return updated
