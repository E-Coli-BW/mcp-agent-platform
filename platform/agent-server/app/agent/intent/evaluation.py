"""Classifier datasets, adapters, and metric contracts."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import httpx

from app.agent.intent.meta import is_meta_question
from app.agent.intent.routing import detect_topic_switch
from app.config import settings

logger = logging.getLogger(__name__)

INTENT_TEST_CASES = [
    {"input": "what model are you using?", "label": "meta"},
    {"input": "who built you?", "label": "meta"},
    {"input": "what tools do you have?", "label": "meta"},
    {"input": "how do you work?", "label": "meta"},
    {"input": "tell me about yourself", "label": "meta"},
    {"input": "are you GPT or Claude?", "label": "meta"},
    {"input": "what's your context window size?", "label": "meta"},
    {"input": "find the JWT authentication code", "label": "task"},
    {"input": "explain how the cache works", "label": "task"},
    {"input": "add error handling to the login function", "label": "task"},
    {"input": "what does this project do?", "label": "task"},
    {"input": "refactor the database connection", "label": "task"},
    {"input": "read the README file", "label": "task"},
    {"input": "why is the test failing?", "label": "task"},
    {"input": "show me the main entry point", "label": "task"},
]

TOPIC_SWITCH_TEST_CASES = [
    {
        "input": "now tell me about the database schema",
        "prev_topic": "JWT auth",
        "label": "switch",
    },
    {
        "input": "actually, can you look at the tests instead?",
        "prev_topic": "main app code",
        "label": "switch",
    },
    {
        "input": "forget that. what's in the config file?",
        "prev_topic": "error handling",
        "label": "switch",
    },
    {
        "input": "by the way, how's the caching implemented?",
        "prev_topic": "user service",
        "label": "switch",
    },
    {"input": "continue", "prev_topic": "reading files", "label": "continue"},
    {"input": "go on", "prev_topic": "code analysis", "label": "continue"},
    {
        "input": "can you also add logging there?",
        "prev_topic": "error handling",
        "label": "continue",
    },
    {
        "input": "what about the error case?",
        "prev_topic": "function analysis",
        "label": "continue",
    },
    {
        "input": "show me more of that file",
        "prev_topic": "file reading",
        "label": "continue",
    },
    {
        "input": "and the tests for it?",
        "prev_topic": "service code",
        "label": "continue",
    },
]

DIRECT_ROUTING_TEST_CASES = [
    {"input": "search my memory for jwt", "label": "memory_search"},
    {"input": "recall what we decided about caching", "label": "memory_search"},
    {"input": "what's in my memory?", "label": "memory_context"},
    {"input": "read README.md", "label": "file_read"},
    {"input": "show me src/main.py", "label": "file_read"},
    {"input": "cat package.json", "label": "file_read"},
    {"input": "list files in src/", "label": "file_list"},
    {"input": "list files", "label": "file_list"},
    {"input": "grep for TODO", "label": "file_search"},
    {"input": "search the code for ConnectionPool", "label": "file_search"},
    {"input": "fix the bug in src/main.py", "label": "fallthrough"},
    {"input": "save this conversation to memory", "label": "fallthrough"},
    {"input": "explain how the cache works", "label": "fallthrough"},
    {"input": "why is the test failing?", "label": "fallthrough"},
    {"input": "refactor the database connection", "label": "fallthrough"},
    {"input": "what does this project do?", "label": "fallthrough"},
    {"input": "delete README.md", "label": "fallthrough"},
    {"input": "read /etc/passwd", "label": "fallthrough"},
]


@dataclass
class EvalResult:
    strategy: str
    task: str
    accuracy: float
    total: int
    correct: int
    latency_ms: float
    errors: list[dict]


def heuristic_direct_routing(text: str) -> str:
    from app.agent.tool_router import classify_for_direct_dispatch

    call = classify_for_direct_dispatch(text)
    return call.tool_name if call is not None else "fallthrough"


def heuristic_intent(text: str) -> str:
    return "meta" if is_meta_question(text) else "task"


def heuristic_topic_switch(text: str, previous_messages: list[dict]) -> str:
    return "switch" if detect_topic_switch(text, previous_messages) else "continue"


async def llm_intent(text: str, model: str = "qwen2.5:0.5b") -> str:
    prompt = f'''Classify this user message as either "meta" (asking about the AI assistant itself) or "task" (asking to do something with code).

Respond with ONLY one word: meta or task

Message: "{text}"
Classification:'''
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.post(
                f"{settings.ollama_base_url}/api/generate",
                json={
                    "model": model,
                    "prompt": prompt,
                    "stream": False,
                    "options": {"temperature": 0.0, "num_predict": 5},
                },
            )
            result = response.json().get("response", "").strip().lower()
            return "meta" if "meta" in result else "task"
    except Exception as error:  # noqa: BLE001 - optional evaluator backend
        logger.debug(
            "LLM intent classification failed; using heuristic: %s",
            error,
        )
        return heuristic_intent(text)


async def llm_topic_switch(
    text: str,
    previous_topic: str,
    model: str = "qwen2.5:0.5b",
) -> str:
    prompt = f'''The user was previously discussing: "{previous_topic}"
Now they say: "{text}"

Is this a topic SWITCH (new unrelated topic) or a CONTINUATION (same topic)?
Respond with ONLY one word: switch or continue

Classification:'''
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.post(
                f"{settings.ollama_base_url}/api/generate",
                json={
                    "model": model,
                    "prompt": prompt,
                    "stream": False,
                    "options": {"temperature": 0.0, "num_predict": 5},
                },
            )
            result = response.json().get("response", "").strip().lower()
            return "switch" if "switch" in result else "continue"
    except Exception:  # noqa: BLE001 - optional evaluator backend
        return heuristic_topic_switch(
            text,
            [{"role": "assistant", "content": previous_topic * 10}],
        )


def _result(
    *,
    strategy: str,
    task: str,
    started_at: float,
    total: int,
    correct: int,
    errors: list[dict],
) -> EvalResult:
    elapsed_ms = (time.time() - started_at) * 1000
    return EvalResult(
        strategy=strategy,
        task=task,
        accuracy=round(correct / total * 100, 1),
        total=total,
        correct=correct,
        latency_ms=round(elapsed_ms / total, 1),
        errors=errors,
    )


async def evaluate_intent_classifiers() -> list[EvalResult]:
    results = []
    for strategy in ("heuristic", "llm_0.5b"):
        correct = 0
        errors = []
        started_at = time.time()
        for case in INTENT_TEST_CASES:
            prediction = (
                heuristic_intent(case["input"])
                if strategy == "heuristic"
                else await llm_intent(case["input"])
            )
            if prediction == case["label"]:
                correct += 1
            else:
                errors.append(
                    {
                        "input": case["input"],
                        "expected": case["label"],
                        "predicted": prediction,
                    }
                )
        results.append(
            _result(
                strategy=strategy,
                task="intent",
                started_at=started_at,
                total=len(INTENT_TEST_CASES),
                correct=correct,
                errors=errors,
            )
        )
    return results


async def evaluate_topic_switch_classifiers() -> list[EvalResult]:
    results = []
    for strategy in ("heuristic", "llm_0.5b"):
        correct = 0
        errors = []
        started_at = time.time()
        for case in TOPIC_SWITCH_TEST_CASES:
            previous_messages = [
                {"role": "assistant", "content": case["prev_topic"] * 100}
            ]
            prediction = (
                heuristic_topic_switch(case["input"], previous_messages)
                if strategy == "heuristic"
                else await llm_topic_switch(case["input"], case["prev_topic"])
            )
            if prediction == case["label"]:
                correct += 1
            else:
                errors.append(
                    {
                        "input": case["input"],
                        "expected": case["label"],
                        "predicted": prediction,
                    }
                )
        results.append(
            _result(
                strategy=strategy,
                task="topic_switch",
                started_at=started_at,
                total=len(TOPIC_SWITCH_TEST_CASES),
                correct=correct,
                errors=errors,
            )
        )
    return results


def evaluate_direct_routing_classifier() -> EvalResult:
    correct = 0
    errors = []
    started_at = time.time()
    for case in DIRECT_ROUTING_TEST_CASES:
        prediction = heuristic_direct_routing(case["input"])
        if prediction == case["label"]:
            correct += 1
        else:
            errors.append(
                {
                    "input": case["input"],
                    "expected": case["label"],
                    "predicted": prediction,
                }
            )
    return _result(
        strategy="regex_router",
        task="direct_routing",
        started_at=started_at,
        total=len(DIRECT_ROUTING_TEST_CASES),
        correct=correct,
        errors=errors,
    )
