"""Generate deterministic Customer QA candidate cases from reviewed templates.

The generator creates candidates only. ``review.status`` remains
``synthetic_pending`` until a human or strong-model review promotes a case to
the golden set.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

SOURCE_URLS = {
    "returns-and-refunds.md": "https://support.stripe.com/questions/refunds-faq?locale=en-GB",
    "refund-status.md": "https://support.stripe.com/questions/how-to-refund-a-customer?locale=en-GB",
    "shipping-status.md": "https://www.ups.com/us/en/support/tracking-support.page",
    "shipping-address.md": "https://www.ups.com/us/en/support/change-delivery-options.page",
    "address-changes.md": "https://help.shopify.com/en/manual/customers/customer-accounts",
    "order-cancellation.md": "https://help.shopify.com/en/manual/orders/manage-orders/cancel-orders",
    "payment-disputes.md": "https://support.stripe.com/questions/disputing-a-purchase-made-through-stripe",
}

# These are paraphrase families based on recurring questions and topics in
# public help centers. They are candidates, not business-policy ground truth.
TEMPLATES = [
    ("returns_policy", "What is the return policy{suffix}?", "returns-and-refunds.md", "shipping-status.md", True),
    ("refund_request", "How do I request a refund{suffix}?", "returns-and-refunds.md", "payment-disputes.md", True),
    ("refund_status", "Why has my refund not appeared yet{suffix}?", "refund-status.md", "returns-and-refunds.md", True),
    ("package_tracking", "How can I track my package{suffix}?", "shipping-status.md", "delivery-exceptions.md", True),
    ("delivery_exception", "What should I do if delivery is delayed{suffix}?", "shipping-status.md", "refund-status.md", True),
    ("shipping_address", "Can I change the delivery address{suffix}?", "shipping-address.md", "address-changes.md", True),
    ("profile_address", "How do I update the address on my account{suffix}?", "address-changes.md", "shipping-address.md", True),
    ("order_cancellation", "Can I cancel my order{suffix}?", "order-cancellation.md", "returns-and-refunds.md", True),
    ("payment_dispute", "I do not recognize this payment; what can I do{suffix}?", "payment-disputes.md", "refund-status.md", True),
    ("order_status", "Can you check the status of my order{suffix}?", None, "shipping-status.md", False),
]
SUFFIXES = [
    "", " for an online purchase", " if the item is damaged", " for an international order",
    " and tell me the exact deadline", " in plain language", " based on the latest policy",
    " when I no longer have the receipt", " if tracking has not changed", " for order 12345",
    " after the package was shipped", " if the card was cancelled", " for a partial amount",
    " when the address is wrong", " if the merchant has not replied", " after two attempts",
    " before the order is fulfilled", " with a guest checkout", " from my mobile phone",
    " and what evidence do I need", " if the status says pending", " when the order is overseas",
    " after changing my profile", " for a duplicate charge", " if the item never arrived",
    " and how long will it take", " without exposing account details", " for an order placed today",
    " when the tracking number is invalid", " if I already opened a support ticket",
]


def validate_cases(cases: list[dict], *, require_reviewed: bool = False) -> list[str]:
    errors = []
    seen = set()
    for case in cases:
        case_id = case.get("id")
        if not case_id or case_id in seen:
            errors.append(f"duplicate_or_missing_id:{case_id}")
        seen.add(case_id)
        for field in ("version", "tenant", "intent", "question", "answerable", "expected_behavior", "review"):
            if field not in case:
                errors.append(f"{case_id}:missing:{field}")
        if not case.get("hard_negatives"):
            errors.append(f"{case_id}:missing:hard_negatives")
        if require_reviewed and case.get("review", {}).get("status") != "human_verified":
            errors.append(f"{case_id}:not_reviewed")
    return errors


def build_cases() -> list[dict]:
    cases = []
    for intent, template, source, negative, answerable in TEMPLATES:
        for index, suffix in enumerate(SUFFIXES, start=1):
            case_id = f"qa-{intent}-{index:02d}"
            cases.append({
                "id": case_id,
                "version": "2026-07-31.v2-public-faq-seeded",
                "tenant": "qa-eval-tenant",
                "intent": intent,
                "difficulty": "hard" if index in {3, 5, 8, 10, 12, 14, 17, 21, 24, 27, 30} else "medium",
                "language": "en",
                "conversation": [],
                "question": template.format(suffix=suffix),
                "answerable": answerable,
                "gold_sources": ([{"document_id": source, "section": "review_required", "source_url": SOURCE_URLS.get(source)}] if source else []),
                "hard_negatives": [
                    {"document_id": negative, "type": "near_miss", "reason": "semantically adjacent intent"},
                    {"document_id": "refund-status.md" if negative != "refund-status.md" else "returns-and-refunds.md", "type": "same-domain-confounder", "reason": "shares refund/order vocabulary"},
                ],
                "expected_behavior": "grounded_answer" if answerable else "handoff_or_refusal",
                "must_cite": [source] if source else [],
                "must_not_claim": ["order status", "refund completed", "account updated"],
                "provenance": {"kind": "public_faq_seeded_paraphrase", "source_urls": sorted(set(SOURCE_URLS.values()))},
                "review": {"status": "synthetic_pending", "reviewer": None, "required": True},
            })
    return cases


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    cases = build_cases()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(cases, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps({"cases": len(cases), "output": str(args.output)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
