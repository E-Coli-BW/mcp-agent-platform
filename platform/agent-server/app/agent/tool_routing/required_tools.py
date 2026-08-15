"""High-confidence user-intent to tool requirements for weak local models.

This is a guardrail, not a replacement planner: it only fires for explicit
requests where answering without the named action would be misleading.
"""
from __future__ import annotations

import re

from app.artifacts import get_artifact_registry

REQUIREMENTS: tuple[tuple[re.Pattern[str], tuple[str, ...]], ...] = (
    (
        re.compile(
            r"\b(?:read|inspect|analy[sz]e|explain)\s+(?:this|the)\s+"
            r"(?:project|codebase|repository|repo)\b",
            re.IGNORECASE,
        ),
        ("file_search", "file_read"),
    ),
    (
        re.compile(
            r"\b(run|execute|calculate|python|script)\b",
            re.IGNORECASE,
        ),
        ("code_run",),
    ),
    (
        re.compile(r"\b(remember|save|store|memorize)\b", re.IGNORECASE),
        ("memory_set",),
    ),
    (
        re.compile(
            r"\b(search|find|look up).*(memory|remembered)\b",
            re.IGNORECASE,
        ),
        ("memory_search",),
    ),
)

_INTERNAL_HUMAN_PREFIXES = ("🚨 STOP.", "⚠️ The previous tool call", "🪞")


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
        r"\b(policy|return|refund|shipping|delivery|warranty|price|faq|what does)\b",
        prompt,
        re.IGNORECASE,
    ):
        return ("rag_search",)
    return ()


def missing_tool_requirement(messages: list[object], available_tools: set[str] | None = None) -> tuple[str, ...]:
    user_prompt = ""
    for message in reversed(messages):
        if getattr(message, "type", None) == "human":
            content = str(getattr(message, "content", ""))
            if not content.startswith(_INTERNAL_HUMAN_PREFIXES):
                user_prompt = content
                break
    required = required_tools_for_agent(user_prompt, available_tools or set()) if available_tools is not None else required_tools(user_prompt)
    observed = set()
    for message in messages:
        for call in getattr(message, "tool_calls", []) or []:
            observed.add(call.get("name"))
    missing = tuple(tool for tool in required if tool not in observed)
    if missing:
        return missing

    artifact_registry = get_artifact_registry()
    tool_outputs = [
        str(getattr(message, "content", ""))
        for message in messages
        if getattr(message, "type", None) == "tool"
    ]
    plugin_required = artifact_registry.required_tools(user_prompt, tool_outputs)
    if available_tools is not None:
        plugin_required = tuple(
            tool_name for tool_name in plugin_required if tool_name in available_tools
        )
    if plugin_required and not artifact_registry.has_verified_artifact(
        user_prompt,
        tool_outputs,
    ):
        unobserved = tuple(
            tool_name for tool_name in plugin_required if tool_name not in observed
        )
        return unobserved or (plugin_required[-1],)

    return ()
