"""LLM-based classifiers vs heuristics — A/B evaluation framework.

This module provides both heuristic and LLM-based implementations for
the same classification tasks, with an evaluation framework to compare them.

PRODUCTION PATTERN:
1. Start with heuristics (fast, debuggable, no GPU cost)
2. Collect labeled data from production traffic
3. Train a small classifier or use a weak LLM
4. A/B test: run both, compare accuracy on labeled set
5. Switch when LLM accuracy > heuristic AND latency is acceptable

Currently implements:
- Intent classification (meta-question vs coding task)
- Topic switch detection (new topic vs continuation)
"""

import asyncio
import json
import time
import logging
from dataclasses import dataclass
from pathlib import Path

import httpx

from app.config import settings
from app.agent.intent import is_meta_question, detect_topic_switch

logger = logging.getLogger(__name__)

# ── Test Cases with Ground Truth Labels ──────────────────────
# These are manually labeled examples for evaluation.
# label: "meta" = meta-question, "task" = coding task, "switch" = topic switch, "continue" = continuation

INTENT_TEST_CASES = [
    # Meta-questions (should NOT use tools)
    {"input": "what model are you using?", "label": "meta"},
    {"input": "who built you?", "label": "meta"},
    {"input": "what tools do you have?", "label": "meta"},
    {"input": "how do you work?", "label": "meta"},
    {"input": "tell me about yourself", "label": "meta"},  # Heuristic might miss this
    {"input": "are you GPT or Claude?", "label": "meta"},
    {"input": "what's your context window size?", "label": "meta"},
    # Coding tasks (should use tools)
    {"input": "find the JWT authentication code", "label": "task"},
    {"input": "explain how the cache works", "label": "task"},
    {"input": "add error handling to the login function", "label": "task"},
    {"input": "what does this project do?", "label": "task"},  # Looks like meta but is task
    {"input": "refactor the database connection", "label": "task"},
    {"input": "read the README file", "label": "task"},
    {"input": "why is the test failing?", "label": "task"},
    {"input": "show me the main entry point", "label": "task"},
]

TOPIC_SWITCH_TEST_CASES = [
    # Topic switches (should reset context)
    {"input": "now tell me about the database schema", "prev_topic": "JWT auth", "label": "switch"},
    {"input": "actually, can you look at the tests instead?", "prev_topic": "main app code", "label": "switch"},
    {"input": "forget that. what's in the config file?", "prev_topic": "error handling", "label": "switch"},
    {"input": "by the way, how's the caching implemented?", "prev_topic": "user service", "label": "switch"},
    # Continuations (should NOT reset context)
    {"input": "continue", "prev_topic": "reading files", "label": "continue"},
    {"input": "go on", "prev_topic": "code analysis", "label": "continue"},
    {"input": "can you also add logging there?", "prev_topic": "error handling", "label": "continue"},
    {"input": "what about the error case?", "prev_topic": "function analysis", "label": "continue"},
    {"input": "show me more of that file", "prev_topic": "file reading", "label": "continue"},
    {"input": "and the tests for it?", "prev_topic": "service code", "label": "continue"},
]


# Labeled set for C2 — direct tool routing.
# label = expected tool name OR "fallthrough" (must NOT route)
#
# Curate from real prompts before quoting accuracy numbers;
# this seed set is for unit testing the classifier, not for production
# set" with the actual number, not extrapolate to "the model is X%
# accurate in production".
DIRECT_ROUTING_TEST_CASES = [
    # Positives — should route
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
    # Negatives — must NOT route (general questions / writes / multi-step)
    {"input": "fix the bug in src/main.py", "label": "fallthrough"},     # write intent
    {"input": "save this conversation to memory", "label": "fallthrough"},  # write intent
    {"input": "explain how the cache works", "label": "fallthrough"},    # general question
    {"input": "why is the test failing?", "label": "fallthrough"},       # diagnosis
    {"input": "refactor the database connection", "label": "fallthrough"},  # multi-step
    {"input": "what does this project do?", "label": "fallthrough"},     # general question
    {"input": "delete README.md", "label": "fallthrough"},               # write intent (catastrophic if mis-routed)
    {"input": "read /etc/passwd", "label": "fallthrough"},               # absolute path refused
]


def heuristic_direct_routing(text: str) -> str:
    """Classify whether the C2 router would dispatch this query.

    Returns the tool name on hit, "fallthrough" on miss. Import is
    deferred so this module stays importable in environments where
    tool_router's deps (langchain_core) are unavailable.
    """
    from app.agent.tool_router import classify_for_direct_dispatch
    call = classify_for_direct_dispatch(text)
    return call.tool_name if call is not None else "fallthrough"


# ── Heuristic Classifiers ────────────────────────────────────

def heuristic_intent(text: str) -> str:
    """Classify intent using regex heuristics (current production code)."""
    return "meta" if is_meta_question(text) else "task"


def heuristic_topic_switch(text: str, prev_messages: list[dict]) -> str:
    """Detect topic switch using heuristics (current production code)."""
    return "switch" if detect_topic_switch(text, prev_messages) else "continue"


# ── LLM-based Classifiers ────────────────────────────────────

async def llm_intent(text: str, model: str = "qwen2.5:0.5b") -> str:
    """Classify intent using a weak LLM (tiny model, ~200ms).
    
    Uses a structured prompt that forces the LLM to respond with exactly
    one word: "meta" or "task". This is cheaper than a full agent loop.
    
    WHY A WEAK MODEL?
    - qwen2.5:0.5b is ~300MB, loads in <1s, infers in ~50ms
    - Classification doesn't need reasoning — it needs pattern matching
    - If the weak model is wrong, the worst case is: meta-question triggers
      an unnecessary agent loop (wastes ~5s) or coding task gets a canned
      response (user just asks again)
    """
    prompt = f"""Classify this user message as either "meta" (asking about the AI assistant itself) or "task" (asking to do something with code).

Respond with ONLY one word: meta or task

Message: "{text}"
Classification:"""

    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.post(
                f"{settings.ollama_base_url}/api/generate",
                json={"model": model, "prompt": prompt, "stream": False,
                      "options": {"temperature": 0.0, "num_predict": 5}},
            )
            result = resp.json().get("response", "").strip().lower()
            return "meta" if "meta" in result else "task"
    except Exception as e:
        logger.debug("LLM intent classification failed, falling back to heuristic: %s", e)
        return heuristic_intent(text)  # Fallback to heuristic


async def llm_topic_switch(text: str, prev_topic: str, model: str = "qwen2.5:0.5b") -> str:
    """Detect topic switch using a weak LLM.
    
    This catches cases the heuristic misses:
    - "what about the error case?" → continuation (heuristic says switch because it starts with "what")
    - "also add logging" → continuation (heuristic says switch because "also")
    """
    prompt = f"""The user was previously discussing: "{prev_topic}"
Now they say: "{text}"

Is this a topic SWITCH (new unrelated topic) or a CONTINUATION (same topic)?
Respond with ONLY one word: switch or continue

Classification:"""

    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.post(
                f"{settings.ollama_base_url}/api/generate",
                json={"model": model, "prompt": prompt, "stream": False,
                      "options": {"temperature": 0.0, "num_predict": 5}},
            )
            result = resp.json().get("response", "").strip().lower()
            return "switch" if "switch" in result else "continue"
    except Exception:
        return heuristic_topic_switch(text, [{"role": "assistant", "content": prev_topic * 10}])


# ── A/B Evaluation Framework ─────────────────────────────────

@dataclass
class EvalResult:
    strategy: str
    task: str
    accuracy: float
    total: int
    correct: int
    latency_ms: float
    errors: list[dict]


async def evaluate_intent_classifiers() -> list[EvalResult]:
    """Run both heuristic and LLM intent classifiers on test set, compare accuracy."""
    results = []

    for strategy_name, classify_fn in [
        ("heuristic", lambda text: heuristic_intent(text)),
        ("llm_0.5b", None),  # async, handled separately
    ]:
        correct = 0
        errors = []
        start = time.time()

        for case in INTENT_TEST_CASES:
            if strategy_name == "heuristic":
                prediction = classify_fn(case["input"])
            else:
                prediction = await llm_intent(case["input"])

            if prediction == case["label"]:
                correct += 1
            else:
                errors.append({
                    "input": case["input"],
                    "expected": case["label"],
                    "predicted": prediction,
                })

        elapsed = (time.time() - start) * 1000
        total = len(INTENT_TEST_CASES)
        results.append(EvalResult(
            strategy=strategy_name, task="intent",
            accuracy=round(correct / total * 100, 1),
            total=total, correct=correct,
            latency_ms=round(elapsed / total, 1),
            errors=errors,
        ))

    return results


async def evaluate_topic_switch_classifiers() -> list[EvalResult]:
    """Run both heuristic and LLM topic switch detectors on test set."""
    results = []

    for strategy_name in ["heuristic", "llm_0.5b"]:
        correct = 0
        errors = []
        start = time.time()

        for case in TOPIC_SWITCH_TEST_CASES:
            prev_messages = [{"role": "assistant", "content": case["prev_topic"] * 100}]

            if strategy_name == "heuristic":
                prediction = heuristic_topic_switch(case["input"], prev_messages)
            else:
                prediction = await llm_topic_switch(case["input"], case["prev_topic"])

            if prediction == case["label"]:
                correct += 1
            else:
                errors.append({
                    "input": case["input"],
                    "expected": case["label"],
                    "predicted": prediction,
                })

        elapsed = (time.time() - start) * 1000
        total = len(TOPIC_SWITCH_TEST_CASES)
        results.append(EvalResult(
            strategy=strategy_name, task="topic_switch",
            accuracy=round(correct / total * 100, 1),
            total=total, correct=correct,
            latency_ms=round(elapsed / total, 1),
            errors=errors,
        ))

    return results


def evaluate_direct_routing_classifier() -> EvalResult:
    """Run the C2 regex router against the labeled DIRECT_ROUTING_TEST_CASES.

    Synchronous because the heuristic itself is sync. If we add an LLM
    router for comparison later (separate function), it'd be async.
    """
    correct = 0
    errors = []
    start = time.time()

    for case in DIRECT_ROUTING_TEST_CASES:
        prediction = heuristic_direct_routing(case["input"])
        if prediction == case["label"]:
            correct += 1
        else:
            errors.append({
                "input": case["input"],
                "expected": case["label"],
                "predicted": prediction,
            })

    elapsed = (time.time() - start) * 1000
    total = len(DIRECT_ROUTING_TEST_CASES)
    return EvalResult(
        strategy="regex_router",
        task="direct_routing",
        accuracy=round(correct / total * 100, 1),
        total=total, correct=correct,
        latency_ms=round(elapsed / total, 1),
        errors=errors,
    )


async def run_full_evaluation():
    """Run all A/B evaluations and print results."""
    print("=" * 70)
    print("  Heuristic vs LLM Classifier — A/B Evaluation")
    print("=" * 70)

    # Intent classification
    print("\n📋 Intent Classification (meta-question vs coding task)")
    print("-" * 70)
    intent_results = await evaluate_intent_classifiers()
    print(f"  {'Strategy':<15} {'Accuracy':>10} {'Correct':>10} {'Latency':>12}")
    for r in intent_results:
        print(f"  {r.strategy:<15} {r.accuracy:>9}% {r.correct}/{r.total:>7} {r.latency_ms:>10.1f}ms")
    for r in intent_results:
        if r.errors:
            print(f"\n  {r.strategy} errors:")
            for e in r.errors:
                print(f"    ❌ '{e['input'][:50]}' → expected={e['expected']}, got={e['predicted']}")

    # Topic switch
    print(f"\n📋 Topic Switch Detection")
    print("-" * 70)
    switch_results = await evaluate_topic_switch_classifiers()
    print(f"  {'Strategy':<15} {'Accuracy':>10} {'Correct':>10} {'Latency':>12}")
    for r in switch_results:
        print(f"  {r.strategy:<15} {r.accuracy:>9}% {r.correct}/{r.total:>7} {r.latency_ms:>10.1f}ms")
    for r in switch_results:
        if r.errors:
            print(f"\n  {r.strategy} errors:")
            for e in r.errors:
                print(f"    ❌ '{e['input'][:50]}' → expected={e['expected']}, got={e['predicted']}")

    # Direct tool routing (C2)
    print(f"\n📋 Direct Tool Routing (C2) — regex fast-path vs LLM hop")
    print("-" * 70)
    routing_result = evaluate_direct_routing_classifier()
    print(f"  {'Strategy':<15} {'Accuracy':>10} {'Correct':>10} {'Latency':>12}")
    print(
        f"  {routing_result.strategy:<15} {routing_result.accuracy:>9}% "
        f"{routing_result.correct}/{routing_result.total:>7} "
        f"{routing_result.latency_ms:>10.2f}ms"
    )
    if routing_result.errors:
        print(f"\n  {routing_result.strategy} errors:")
        for e in routing_result.errors:
            print(f"    ❌ '{e['input'][:50]}' → expected={e['expected']}, got={e['predicted']}")
    print(
        f"\n  💡 Each correct routing skips one LLM hop (~800ms / ~$0.02). "
        f"Each incorrect routing in this set is a misclassification — would "
        f"either waste a tool call (false positive) or incur a needless LLM "
        f"hop (false negative)."
    )

    # Summary
    print(f"\n{'=' * 70}")
    print("  Summary & Recommendation")
    print("=" * 70)
    for task in ["intent", "topic_switch"]:
        task_results = [r for r in intent_results + switch_results if r.task == task]
        if len(task_results) == 2:
            h, l = task_results[0], task_results[1]
            delta = l.accuracy - h.accuracy
            print(f"\n  {task}:")
            print(f"    Heuristic: {h.accuracy}% accuracy, {h.latency_ms:.0f}ms")
            print(f"    LLM:       {l.accuracy}% accuracy, {l.latency_ms:.0f}ms")
            if delta > 5:
                print(f"    ✅ LLM is {delta:.0f}% better — worth the latency cost")
            elif delta > 0:
                print(f"    🟡 LLM is {delta:.0f}% better — marginal, keep heuristic for speed")
            else:
                print(f"    ❌ Heuristic is equal or better — no reason to use LLM")

    # Save results
    all_results = [
        {"strategy": r.strategy, "task": r.task, "accuracy": r.accuracy,
         "latency_ms": r.latency_ms, "errors": r.errors}
        for r in intent_results + switch_results + [routing_result]
    ]
    results_file = Path.home() / ".mcp-local" / "classifier-eval-results.json"
    results_file.parent.mkdir(parents=True, exist_ok=True)
    results_file.write_text(json.dumps(all_results, indent=2))
    print(f"\n💾 Results saved to {results_file}")


if __name__ == "__main__":
    asyncio.run(run_full_evaluation())
