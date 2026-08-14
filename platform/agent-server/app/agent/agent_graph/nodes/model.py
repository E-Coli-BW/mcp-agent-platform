"""Model-call node and response normalization for the v2 Agent graph."""

from __future__ import annotations

import json
import logging
import time

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.tools import BaseTool
from langchain_core.utils.function_calling import convert_to_openai_tool

from app.agent.agent_graph.nodes.context import _build_llm_view
from app.agent.agent_graph.state import AgentState, ContextBudgetExceededError
from app.agent.compressor import update_investigation_from_messages
from app.agent.model_recovery import invoke_with_model_retry, policy_from_settings
from app.config import settings

logger = logging.getLogger(__name__)


def _deduplicate_tool_calls(response: BaseMessage) -> BaseMessage:
    """Keep the first identical tool request within a single model turn."""
    if not isinstance(response, AIMessage) or len(response.tool_calls) < 2:
        return response

    seen: set[tuple[str, str]] = set()
    unique_calls = []
    for tool_call in response.tool_calls:
        signature = (
            tool_call.get("name", ""),
            json.dumps(tool_call.get("args", {}), sort_keys=True, default=str),
        )
        if signature in seen:
            continue
        seen.add(signature)
        unique_calls.append(tool_call)

    if len(unique_calls) == len(response.tool_calls):
        return response
    logger.warning(
        "Dropped %d duplicate tool call(s) from one model turn",
        len(response.tool_calls) - len(unique_calls),
    )
    return response.model_copy(update={"tool_calls": unique_calls})


def make_call_llm_node(
    model: BaseChatModel,
    tools: list[BaseTool],
    system_prompt: str,
):
    """Create the bounded, recoverable model-call node."""
    model_with_tools = model.bind_tools(tools) if tools else model
    request_identity = {
        "tools": [convert_to_openai_tool(tool) for tool in tools],
    }

    async def call_llm_node(state: AgentState):
        view = _build_llm_view(state, system_prompt)
        if view.overflow:
            raise ContextBudgetExceededError(
                "Required context floor exceeds budget: "
                f"{view.total_chars} > {view.budget_chars} chars"
            )
        logger.info(
            "LLM call [loop=%d, msgs=%d, chars=%d, errors=%d]",
            state.loop_counter + 1,
            len(view.messages),
            view.total_chars,
            state.consecutive_errors,
        )
        logger.debug(
            "LLM message types: %s",
            [type(message).__name__ for message in view.messages],
        )

        started_at = time.monotonic()
        response = _deduplicate_tool_calls(
            await invoke_with_model_retry(
                model_with_tools,
                view.messages,
                policy=policy_from_settings(settings),
                request_identity=request_identity,
            )
        )
        elapsed_ms = int((time.monotonic() - started_at) * 1000)
        if isinstance(response, AIMessage) and response.tool_calls:
            tool_names = [call.get("name", "?") for call in response.tool_calls]
            logger.info(
                "LLM requested %d tool(s): %s [%dms]",
                len(tool_names),
                tool_names,
                elapsed_ms,
            )
        else:
            content = str(response.content)
            preview = content[:120] + "..." if len(content) > 120 else content
            logger.info(
                "LLM returned text [%dms, %d chars]: %s",
                elapsed_ms,
                len(content),
                preview,
            )

        investigation = state.investigation.model_copy(deep=True)
        update_investigation_from_messages(investigation, [response])
        return {
            "messages": [response],
            "loop_counter": state.loop_counter + 1,
            "investigation": investigation,
            "compression_summary": view.compression_summary,
        }

    return call_llm_node