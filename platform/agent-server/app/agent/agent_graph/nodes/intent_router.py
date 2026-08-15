"""LangGraph adapter for conservative direct tool dispatch."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.tools import BaseTool

from app.agent.tool_routing.direct_dispatch import (
    READ_ONLY_TOOL_ALLOWLIST,
    classify_for_direct_dispatch,
)

logger = logging.getLogger(__name__)


def _find_latest_user_text(messages: list[BaseMessage]) -> str | None:
    """Find the latest end-user message, ignoring internal critique markers."""
    for message in reversed(messages):
        if not isinstance(message, HumanMessage):
            continue
        content = (
            message.content
            if isinstance(message.content, str)
            else str(message.content)
        )
        if not content.startswith("🪞"):
            return content
    return None


def make_intent_router_node(tools: list[BaseTool]):
    """Create the first-hop graph node for high-confidence read-only calls."""
    tool_by_name = {
        tool.name: tool
        for tool in tools
        if tool.name in READ_ONLY_TOOL_ALLOWLIST
    }
    if not tool_by_name:
        logger.info(
            "intent_router: NO allowlisted read-only tools bound; "
            "router will always use the model"
        )

    async def intent_router(state: Any) -> dict[str, Any]:
        from app.config import settings

        if not settings.direct_tool_routing_enabled:
            return {}
        messages = list(state.messages)
        if not messages or not isinstance(messages[-1], HumanMessage):
            return {}

        text = _find_latest_user_text(messages)
        if not text:
            return {}
        last_content = (
            messages[-1].content
            if isinstance(messages[-1].content, str)
            else str(messages[-1].content)
        )
        if last_content.startswith("🪞"):
            return {}

        call = classify_for_direct_dispatch(text)
        if (
            call is None
            or call.tool_name not in READ_ONLY_TOOL_ALLOWLIST
            or call.tool_name not in tool_by_name
        ):
            return {}

        tool_call_id = f"router_{uuid.uuid4().hex[:12]}"
        synthetic = AIMessage(
            content="",
            tool_calls=[
                {
                    "id": tool_call_id,
                    "name": call.tool_name,
                    "args": dict(call.args),
                }
            ],
            additional_kwargs={
                "router_dispatched": True,
                "router_rule": call.rule_id,
            },
        )
        logger.info(
            "router -> %s(%s) [rule=%s, tool_call_id=%s]",
            call.tool_name,
            call.args,
            call.rule_id,
            tool_call_id,
        )
        return {"messages": [synthetic]}

    return intent_router


def route_after_intent_router(state: Any) -> str:
    """Route a synthetic dispatch to tools; otherwise use the model."""
    messages = list(state.messages)
    if not messages:
        return "call_llm"
    last = messages[-1]
    if (
        isinstance(last, AIMessage)
        and last.tool_calls
        and last.additional_kwargs.get("router_dispatched")
    ):
        return "tools"
    return "call_llm"