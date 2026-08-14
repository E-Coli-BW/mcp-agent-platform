"""Tool error boundaries, output limits, and error-state tracking."""

from __future__ import annotations

import asyncio
import logging
import subprocess
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import BaseTool, StructuredTool

from app.agent.agent_graph.state import AgentState

logger = logging.getLogger(__name__)


def _format_tool_error(error: Exception) -> str:
    """Convert ordinary tool failures into bounded model-visible results."""
    code = (
        "TOOL_TIMEOUT"
        if isinstance(
            error,
            (TimeoutError, asyncio.TimeoutError, subprocess.TimeoutExpired),
        )
        else "TOOL_ERROR"
    )
    detail = str(error).strip() or type(error).__name__
    if len(detail) > 2000:
        detail = detail[:2000] + "... (truncated)"
    return f"Error [{code}]: {type(error).__name__}: {detail}"


def make_error_tracking_node():
    """Create the post-tool node that updates the typed error streak."""

    def track_errors(state: AgentState) -> dict[str, Any]:
        recent_tool_messages = []
        for message in reversed(state.messages):
            if isinstance(message, ToolMessage):
                recent_tool_messages.append(message)
            elif isinstance(message, AIMessage):
                break

        for message in recent_tool_messages:
            content = (
                message.content
                if isinstance(message.content, str)
                else str(message.content)
            )
            preview = content[:150] + "..." if len(content) > 150 else content
            logger.info(
                "Tool result [%s, %d chars]: %s",
                message.name or "?",
                len(content),
                preview.replace("\n", " "),
            )

        has_error = False
        for message in recent_tool_messages:
            if getattr(message, "status", None) == "error":
                has_error = True
                break
            content = (
                message.content
                if isinstance(message.content, str)
                else str(message.content)
            )
            if content.startswith("❌"):
                has_error = True
                break

        if has_error:
            new_count = state.consecutive_errors + 1
            logger.warning("Tool error detected (consecutive: %d/3)", new_count)
            if new_count <= 3:
                return {
                    "consecutive_errors": new_count,
                    "messages": [
                        HumanMessage(
                            content=(
                                "⚠️ The previous tool call returned an error. "
                                "Review the error and try a different approach. "
                                f"(attempt {new_count}/3)"
                            )
                        )
                    ],
                }
            return {"consecutive_errors": new_count}
        return {"consecutive_errors": 0}

    return track_errors


def _truncate_tool_output(tool: BaseTool, max_chars: int) -> BaseTool:
    """Wrap a tool with a public-API output limit."""
    inner = tool

    def truncate(result: Any) -> Any:
        if isinstance(result, str) and len(result) > max_chars:
            omitted = len(result) - max_chars
            return (
                result[:max_chars]
                + f"\n\n... (truncated, {omitted} chars omitted. "
                + "Use start_line/end_line for specific ranges)"
            )
        return result

    async def wrapped_async(**kwargs):
        return truncate(await inner.ainvoke(kwargs))

    def wrapped_sync(**kwargs):
        return truncate(inner.invoke(kwargs))

    return StructuredTool.from_function(
        func=wrapped_sync,
        coroutine=wrapped_async,
        name=inner.name,
        description=inner.description,
        args_schema=inner.args_schema,
        response_format=getattr(inner, "response_format", "content"),
    )