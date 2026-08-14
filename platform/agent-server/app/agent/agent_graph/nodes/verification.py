"""Artifact synthesis and bounded tool-protocol correction nodes."""

from __future__ import annotations

from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.agent.agent_graph.state import AgentState
from app.artifacts import ArtifactRegistry, get_artifact_registry


def make_synthesize_artifact_node(
    registry: ArtifactRegistry | None = None,
):
    """Create a node that accepts verified plugin artifacts."""
    artifact_registry = registry or get_artifact_registry()

    def synthesize_artifact(state: AgentState) -> dict[str, Any]:
        user_prompt = ""
        for message in reversed(state.messages):
            if isinstance(message, HumanMessage):
                content = str(message.content)
                if not content.startswith(("🚨 STOP.", "⚠️ The previous tool call", "🪞")):
                    user_prompt = content
                    break

        tool_outputs = [
            str(message.content)
            for message in state.messages
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
        from app.agent.tool_contract import missing_tool_requirement

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