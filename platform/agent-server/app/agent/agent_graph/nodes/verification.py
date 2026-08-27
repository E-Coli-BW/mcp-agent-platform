"""Artifact synthesis and bounded tool-protocol correction nodes."""

from __future__ import annotations

import json
import posixpath
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.agent.agent_graph.state import AgentState
from app.agent.tool_routing.direct_dispatch import READ_ONLY_TOOL_ALLOWLIST
from app.agent.tool_routing.required_tools import (
    current_turn_messages,
    tool_message_is_error,
)
from app.artifacts import ArtifactRegistry, get_artifact_registry

_FINALIZE_PREFIX = "🚨 FINALIZE."


def _read_call_signature(call: dict[str, Any]) -> tuple[str, str]:
    name = str(call.get("name", ""))
    arguments = dict(call.get("args", {}))
    for key in ("path", "directory"):
        value = arguments.get(key)
        if isinstance(value, str):
            arguments[key] = posixpath.normpath(value)
    return name, json.dumps(arguments, sort_keys=True, default=str)


def repeats_completed_read_only_call(messages: list[Any]) -> bool:
    """Return whether the final AI turn only repeats completed read calls."""
    messages = current_turn_messages(messages)
    if not messages or not isinstance(messages[-1], AIMessage):
        return False
    current_calls = messages[-1].tool_calls
    if not current_calls:
        return False

    calls_by_id: dict[str, tuple[str, str]] = {}
    completed: set[tuple[str, str]] = set()
    for message in messages[:-1]:
        if isinstance(message, AIMessage):
            for call in message.tool_calls:
                name = str(call.get("name", ""))
                call_id = str(call.get("id", ""))
                if name in READ_ONLY_TOOL_ALLOWLIST and call_id:
                    calls_by_id[call_id] = _read_call_signature(call)
        elif isinstance(message, ToolMessage):
            if tool_message_is_error(message):
                continue
            signature = calls_by_id.get(str(message.tool_call_id))
            if signature is not None:
                completed.add(signature)

    current_signatures = {_read_call_signature(call) for call in current_calls}
    return bool(current_signatures) and current_signatures <= completed


def make_synthesize_artifact_node(
    registry: ArtifactRegistry | None = None,
):
    """Create a node that accepts verified plugin artifacts."""
    artifact_registry = registry or get_artifact_registry()

    def synthesize_artifact(state: AgentState) -> dict[str, Any]:
        messages = current_turn_messages(list(state.messages))
        user_prompt = ""
        for message in reversed(messages):
            if isinstance(message, HumanMessage):
                content = str(message.content)
                if not content.startswith(("🚨 STOP.", "⚠️ The previous tool call", "🪞")):
                    user_prompt = content
                    break

        tool_outputs = [
            str(message.content)
            for message in messages
            if isinstance(message, ToolMessage)
        ]
        selected = artifact_registry.select_for_prompt(
            prompt=user_prompt,
            tool_outputs=tool_outputs,
        )
        if selected is None:
            return {}
        plugin, artifact = selected
        answer = artifact_registry.render_answer(plugin, artifact)
        if not answer:
            return {}
        return {
            "messages": [
                AIMessage(
                    content=answer,
                    additional_kwargs={
                        "verified_artifact": {
                            "plugin_id": artifact.plugin_id,
                            "artifact_type": artifact.artifact_type,
                            "source_path": artifact.source_path,
                        }
                    },
                )
            ]
        }

    return synthesize_artifact


def make_hallucination_fix_node(available_tools: set[str] | None = None):
    """Create one bounded correction for missing structured tool calls."""
    correction = (
        "🚨 STOP. You either described or promised tool use, or attempted to finish "
        "without collecting required evidence. The required action was NOT executed. "
        "You MUST use the function calling interface to invoke tools — "
        "do NOT write tool names or commands in your text response. "
        "Try again: call the appropriate tool NOW using a proper function call."
    )

    def fix_hallucination(state: AgentState) -> dict[str, Any]:
        from app.agent.tool_routing.required_tools import missing_tool_requirement

        if repeats_completed_read_only_call(list(state.messages)):
            return {
                "messages": [
                    HumanMessage(
                        content=(
                            f"{_FINALIZE_PREFIX} The requested read-only evidence was "
                            "already collected. Do not call any more tools. Answer the "
                            "user now with a concise synthesis grounded in the available "
                            "tool results."
                        )
                    )
                ]
            }

        missing = missing_tool_requirement(list(state.messages), available_tools)
        suffix = (
            f" Required tool(s) for this explicit request: {', '.join(missing)}."
            if missing
            else ""
        )
        if "file_read" in missing:
            suffix += (
                " Read a new, relevant range from the matched definition. For workflows, "
                "collect entry, transition, branch/error, and terminal-state evidence; "
                "do not reread the same snippet."
            )
        return {"messages": [HumanMessage(content=correction + suffix)]}

    return fix_hallucination