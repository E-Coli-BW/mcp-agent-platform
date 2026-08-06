"""High-confidence user-intent to tool requirements for weak local models.

This is a guardrail, not a replacement planner: it only fires for explicit
requests where answering without the named action would be misleading.
"""
from __future__ import annotations

import re

REQUIREMENTS: tuple[tuple[re.Pattern[str], tuple[str, ...]], ...] = (
    (re.compile(r"\b(run|execute|calculate|python|script)\b", re.I), ("code_run",)),
    (re.compile(r"\b(remember|save|store|memorize)\b", re.I), ("memory_set",)),
    (re.compile(r"\b(search|find|look up).*(memory|remembered)\b", re.I), ("memory_search",)),
)


def required_tools(prompt: str) -> tuple[str, ...]:
    for pattern, tools in REQUIREMENTS:
        if pattern.search(prompt):
            return tools
    return ()


def required_tools_for_agent(prompt: str, available_tools: set[str]) -> tuple[str, ...]:
    """Return only requirements that this agent is allowed to satisfy."""
    required = required_tools(prompt)
    if required:
        return tuple(tool for tool in required if tool in available_tools)
    if "rag_search" in available_tools and re.search(
        r"\b(policy|return|refund|shipping|delivery|warranty|price|faq|what does)\b", prompt, re.I
    ):
        return ("rag_search",)
    return ()


def missing_tool_requirement(messages: list[object], available_tools: set[str] | None = None) -> tuple[str, ...]:
    user_prompt = ""
    for message in reversed(messages):
        if getattr(message, "type", None) == "human":
            user_prompt = str(getattr(message, "content", ""))
            break
    required = required_tools_for_agent(user_prompt, available_tools or set()) if available_tools is not None else required_tools(user_prompt)
    observed = set()
    for message in messages:
        for call in getattr(message, "tool_calls", []) or []:
            observed.add(call.get("name"))
    return tuple(tool for tool in required if tool not in observed)
