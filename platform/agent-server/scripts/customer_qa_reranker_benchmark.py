"""Compare Customer QA rerankers over a fixed, reproducible candidate set."""
from __future__ import annotations

import json
import time
from pathlib import Path

from app.rag.index.retriever import ScoredChunk
from app.rag.reranking.cross_encoder import cross_encoder_rerank
from app.rag.reranking.dispatcher import heuristic_rerank

CASES = [
    ("How do I return a damaged item?", "returns-and-refunds.md"),
    ("How can I request a refund?", "returns-and-refunds.md"),
    ("Where is my package and how do I track it?", "shipping-status.md"),
    ("How do I change my mailing address?", "address-changes.md"),
    ("Can I update the address after placing an order?", "address-changes.md"),
    ("What evidence is needed when an item arrived damaged?", "returns-and-refunds.md"),
    ("Has my refund been issued yet?", "refund-status.md"),
    ("Can I cancel an order before it ships?", "order-cancellation.md"),
    ("Can I change the delivery address after checkout?", "shipping-address.md"),
    ("Where can I see the tracking number?", "shipping-status.md"),
    ("Is a damaged product eligible for a return?", "returns-and-refunds.md"),
    ("How do I edit my saved profile address?", "address-changes.md"),
    ("Why does the refund page say pending?", "refund-status.md"),
    ("Can an order be stopped after it has shipped?", "order-cancellation.md"),
    ("What happens if the shipping address is wrong?", "shipping-address.md"),
    ("Does changing my profile address change an existing order?", "address-changes.md"),
    ("How long does a return take before a refund is processed?", "returns-and-refunds.md"),
    ("How can I tell whether the parcel was delivered?", "shipping-status.md"),
]
PASSAGES = {
    "returns-and-refunds.md": "Returns and refunds: customers can request a return or refund for eligible purchases. Damaged items may require additional evidence.",
    "shipping-status.md": "Shipping status: track a package using its tracking information and review delivery updates.",
    "address-changes.md": "Address changes: update a mailing address through the supported change-of-address process.",
    "shipping-address.md": "Shipping address information is shown on the order confirmation and may differ from the current profile address.",
    "refund-status.md": "Refund status explains when an approved refund is issued; it does not define return eligibility or damage evidence.",
    "order-cancellation.md": "Order cancellation is a separate process from returning a delivered or damaged item.",
    "delivery-exceptions.md": "Delivery exceptions cover failed delivery attempts, damaged packaging, and carrier delays.",
    "exchange-policy.md": "Exchanges replace an item and are different from a refund or a return request.",
    "payment-methods.md": "Payment methods and authorization explain how an order is charged at checkout.",
    "account-security.md": "Account security includes changing credentials and verifying account ownership.",
    "warranty-claims.md": "Warranty claims cover eligible product defects and may require proof of purchase.",
}


def candidates():
    return [ScoredChunk(text, name, "markdown", "section", name, 1, 4, score=0.1, metadata={}) for name, text in PASSAGES.items()]


def evaluate(reranker, label):
    hits = 0
    ranks = []
    total_ms = 0.0
    per_case = []
    for query, expected in CASES:
        items = candidates()
        start = time.perf_counter()
        ranked = reranker(query, items, top_k=3)
        total_ms += (time.perf_counter() - start) * 1000
        names = [item.file_path for item in ranked]
        if expected in names:
            hits += 1
            rank = names.index(expected) + 1
            ranks.append(1 / rank)
        else:
            rank = None
            ranks.append(0.0)
        per_case.append({"query": query, "expected": expected, "rank": rank, "top": names[0] if names else None})
    return {"label": label, "recall_at_3": round(hits / len(CASES), 3), "mrr": round(sum(ranks) / len(ranks), 3), "avg_latency_ms": round(total_ms / len(CASES), 1), "per_case": per_case}


def main():
    results = [evaluate(lambda q, c, top_k: c[:top_k], "none"), evaluate(heuristic_rerank, "heuristic"), evaluate(cross_encoder_rerank, "cross_encoder")]
    output = Path.home() / ".mcp-local" / "customer-qa-reranker-benchmark.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({"cases": len(CASES), "results": results}, indent=2))
    print(json.dumps({"cases": len(CASES), "results": results}, ensure_ascii=False))


if __name__ == "__main__":
    main()
