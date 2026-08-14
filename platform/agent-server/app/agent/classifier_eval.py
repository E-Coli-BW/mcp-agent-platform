"""Compatibility exports and CLI for intent classifier evaluation."""

from __future__ import annotations

import asyncio

from app.agent.intent.eval_cli import run_full_evaluation
from app.agent.intent.evaluation import (
    DIRECT_ROUTING_TEST_CASES,
    INTENT_TEST_CASES,
    TOPIC_SWITCH_TEST_CASES,
    EvalResult,
    evaluate_direct_routing_classifier,
    evaluate_intent_classifiers,
    evaluate_topic_switch_classifiers,
    heuristic_direct_routing,
    heuristic_intent,
    heuristic_topic_switch,
    httpx,
    llm_intent,
    llm_topic_switch,
)

__all__ = [
    "DIRECT_ROUTING_TEST_CASES",
    "INTENT_TEST_CASES",
    "TOPIC_SWITCH_TEST_CASES",
    "EvalResult",
    "evaluate_direct_routing_classifier",
    "evaluate_intent_classifiers",
    "evaluate_topic_switch_classifiers",
    "heuristic_direct_routing",
    "heuristic_intent",
    "heuristic_topic_switch",
    "httpx",
    "llm_intent",
    "llm_topic_switch",
    "run_full_evaluation",
]

if __name__ == "__main__":
    asyncio.run(run_full_evaluation())
