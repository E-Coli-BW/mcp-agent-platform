"""Model-call node and response normalization for the v2 Agent graph."""

from __future__ import annotations

import hashlib
import json
import logging
import posixpath
import time

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.tools import BaseTool
from langchain_core.utils.function_calling import convert_to_openai_tool

from app.agent.agent_graph.nodes.context import _build_llm_view
from app.agent.agent_graph.state import AgentState, ContextBudgetExceededError
from app.agent.compressor import update_investigation_from_messages
from app.agent.model.request_recovery import (
    invoke_with_model_retry,
    policy_from_settings,
)
from app.agent.tool_routing.direct_dispatch import READ_ONLY_TOOL_ALLOWLIST
from app.agent.tool_routing.required_tools import current_turn_messages
from app.config import settings

logger = logging.getLogger(__name__)

_RECOVERABLE_TOOL_PREFIXES = {"", "<tool_call>", "portun"}
_RECOVERABLE_TOOL_SUFFIXES = {"", "</tool_call>"}
_TOOL_BUDGET_FINALIZE_INSTRUCTION = (
    "The tool-call budget is exhausted. Do not call or describe any more tools. "
    "Complete the user's request now using the evidence and tool results already "
    "available. State honestly if any requested action could not be completed."
)


def _recover_read_only_text_tool_call(
    response: BaseMessage,
    available_tools: set[str],
) -> BaseMessage:
    """Recover one exact read-only Qwen tool envelope emitted as text."""
    if not isinstance(response, AIMessage) or response.tool_calls:
        return response
    if not isinstance(response.content, str):
        return response

    content = response.content.strip()
    object_start = content.find("{")
    if object_start < 0:
        return response
    prefix = content[:object_start].strip()
    if prefix not in _RECOVERABLE_TOOL_PREFIXES:
        return response

    try:
        payload, object_end = json.JSONDecoder().raw_decode(content[object_start:])
    except json.JSONDecodeError:
        return response
    suffix = content[object_start + object_end :].strip()
    if suffix not in _RECOVERABLE_TOOL_SUFFIXES or not isinstance(payload, dict):
        return response
    if set(payload) != {"name", "arguments"}:
        return response

    tool_name = payload.get("name")
    arguments = payload.get("arguments")
    if (
        not isinstance(tool_name, str)
        or tool_name not in READ_ONLY_TOOL_ALLOWLIST
        or tool_name not in available_tools
        or not isinstance(arguments, dict)
    ):
        return response
    encoded_arguments = json.dumps(arguments, sort_keys=True, default=str)
    if len(encoded_arguments) > 2_000:
        return response

    call_hash = hashlib.sha256(
        f"{tool_name}:{encoded_arguments}".encode()
    ).hexdigest()[:16]
    additional_kwargs = dict(response.additional_kwargs)
    additional_kwargs["recovered_text_tool_call"] = True
    logger.warning("Recovered malformed read-only tool call: %s", tool_name)
    return response.model_copy(
        update={
            "content": "",
            "tool_calls": [
                {
                    "id": f"recovered-{call_hash}",
                    "name": tool_name,
                    "args": arguments,
                    "type": "tool_call",
                }
            ],
            "additional_kwargs": additional_kwargs,
        }
    )


def _deduplicate_tool_calls(response: BaseMessage) -> BaseMessage:
    """Keep the first identical tool request within a single model turn."""
    if not isinstance(response, AIMessage) or len(response.tool_calls) < 2:
        return response

    seen: set[tuple[str, str]] = set()
    unique_calls = []
    for tool_call in response.tool_calls:
        name = str(tool_call.get("name", ""))
        arguments = dict(tool_call.get("args", {}))
        if name in READ_ONLY_TOOL_ALLOWLIST:
            for key in ("path", "directory"):
                value = arguments.get(key)
                if isinstance(value, str):
                    arguments[key] = posixpath.normpath(value)
        signature = (
            name,
            json.dumps(arguments, sort_keys=True, default=str),
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


def _current_turn_tool_call_count(messages: list[BaseMessage]) -> int:
    return sum(
        len(message.tool_calls)
        for message in current_turn_messages(messages)
        if isinstance(message, AIMessage)
    )


def _limit_tool_calls_to_budget(
    response: BaseMessage,
    messages: list[BaseMessage],
    max_tool_calls: int,
) -> BaseMessage:
    """Keep only tool calls that fit this turn's remaining hard budget."""
    if not isinstance(response, AIMessage) or not response.tool_calls:
        return response
    used = _current_turn_tool_call_count(messages)
    remaining = max(0, max_tool_calls - used)
    if len(response.tool_calls) <= remaining:
        return response

    retained = response.tool_calls[:remaining]
    dropped = len(response.tool_calls) - len(retained)
    logger.warning(
        "Dropped %d tool call(s) beyond remaining turn budget (%d/%d already used)",
        dropped,
        used,
        max_tool_calls,
    )
    additional_kwargs = dict(response.additional_kwargs)
    additional_kwargs["tool_calls_dropped_for_budget"] = dropped
    return response.model_copy(
        update={
            "tool_calls": retained,
            "additional_kwargs": additional_kwargs,
        }
    )


def make_call_llm_node(
    model: BaseChatModel,
    tools: list[BaseTool],
    system_prompt: str,
    max_context_chars: int | None = None,
    max_tool_calls: int | None = None,
):
    """Create the bounded, recoverable model-call node."""
    model_with_tools = model.bind_tools(tools) if tools else model
    available_tool_names = {tool.name for tool in tools}
    request_identity = {
        "tools": [convert_to_openai_tool(tool) for tool in tools],
    }

    async def call_llm_node(state: AgentState):
        used_tool_calls = _current_turn_tool_call_count(list(state.messages))
        finalize_for_budget = (
            max_tool_calls is not None and used_tool_calls >= max_tool_calls
        )
        effective_system_prompt = system_prompt
        if finalize_for_budget:
            effective_system_prompt += (
                "\n\nRUNTIME FINALIZATION:\n"
                + _TOOL_BUDGET_FINALIZE_INSTRUCTION
            )
        view = _build_llm_view(
            state,
            effective_system_prompt,
            max_context_chars=max_context_chars,
        )
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
        response = await invoke_with_model_retry(
            model if finalize_for_budget else model_with_tools,
            view.messages,
            policy=policy_from_settings(settings),
            request_identity={"tools": []} if finalize_for_budget else request_identity,
        )
        if finalize_for_budget:
            if isinstance(response, AIMessage):
                additional_kwargs = dict(response.additional_kwargs)
                additional_kwargs["tool_budget_finalized"] = True
                response = response.model_copy(
                    update={
                        "tool_calls": [],
                        "additional_kwargs": additional_kwargs,
                    }
                )
        else:
            response = _limit_tool_calls_to_budget(
                _deduplicate_tool_calls(
                    _recover_read_only_text_tool_call(
                        response,
                        available_tool_names,
                    )
                ),
                list(state.messages),
                max_tool_calls if max_tool_calls is not None else settings.max_agent_steps,
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