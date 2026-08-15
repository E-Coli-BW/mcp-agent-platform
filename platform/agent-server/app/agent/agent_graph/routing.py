"""Typed routing decisions and terminal guards for the v2 Agent graph."""

from __future__ import annotations

import logging
import re
from typing import Literal

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.agent.agent_graph.state import AgentState, AgentTerminationError

logger = logging.getLogger(__name__)

_HALLUCINATED_TOOL_RE = re.compile(
    r"(?:Using |🔧 Using |📝 |🔍 )?"
    r"(file_write|file_read|file_list|file_edit|file_search|"
    r"code_run|code_shell|git_commit|git_diff|git_status|run_tests|"
    r"memory_set|memory_search|memory_context|rag_search)"
    r"[\s.:(]",
    re.IGNORECASE,
)
_UNFINISHED_ACTION_RE = re.compile(
    r"\b(?:I\s+will|I'll|Let\s+me|Let's|"
    r"Next,?\s+(?:I\s+will|I'll|let's)|The\s+next\s+step\s+is\s+to)\s+"
    r"(?:now\s+)?(?:perform\s+(?:a\s+)?(?:grep\s+)?search|search|grep|read|"
    r"inspect|examine|explore|list|check|open|look\s+for|use)\b",
    re.IGNORECASE,
)
_EXPLICIT_TOOL_SYNTAX_RE = re.compile(
    r"<tool_call>|"
    r'"name"\s*:\s*"(?:file_|code_|run_tests|git_|memory_|rag_)[^"]*"|'
    r'"arguments"\s*:',
    re.IGNORECASE,
)


def route_after_artifact_synthesis(
    state: AgentState,
) -> Literal["call_llm", "__end__"]:
    last = state.messages[-1] if state.messages else None
    if (
        isinstance(last, AIMessage)
        and isinstance(last.additional_kwargs.get("verified_artifact"), dict)
    ):
        return "__end__"
    return "call_llm"


def make_route_node(max_steps: int, available_tools: set[str] | None = None):
    """Create the routing function for tool, correction, revision, and end paths."""

    def route(
        state: AgentState,
    ) -> Literal["tools", "call_llm", "hallucination_fix", "__end__"]:
        if state.loop_counter >= max_steps:
            raise AgentTerminationError(
                "max_steps_exceeded",
                f"agent exhausted its {max_steps}-step budget",
            )
        if state.consecutive_errors > 3:
            raise AgentTerminationError(
                "consecutive_tool_errors_exceeded",
                f"agent stopped after {state.consecutive_errors} consecutive tool errors",
            )

        last = state.messages[-1] if state.messages else None
        if isinstance(last, AIMessage) and last.tool_calls:
            return "tools"

        if isinstance(last, AIMessage) and not last.tool_calls:
            from app.agent.tool_routing.required_tools import missing_tool_requirement

            previous = state.messages[-2] if len(state.messages) >= 2 else None
            has_recent_tool_result = isinstance(previous, ToolMessage)
            content = (
                last.content if isinstance(last.content, str) else str(last.content)
            )
            missing = missing_tool_requirement(list(state.messages), available_tools)
            if missing and state.loop_counter < max_steps - 1:
                return "hallucination_fix"

            matches = _HALLUCINATED_TOOL_RE.findall(content)
            fake_tool_use = (
                bool(_EXPLICIT_TOOL_SYNTAX_RE.search(content))
                or bool(_UNFINISHED_ACTION_RE.search(content))
                or (bool(matches) and not has_recent_tool_result)
            )
            if fake_tool_use and state.loop_counter < max_steps - 1:
                correction_already_sent = (
                    isinstance(previous, HumanMessage)
                    and str(previous.content).startswith("🚨 STOP.")
                )
                if correction_already_sent:
                    raise AgentTerminationError(
                        "tool_protocol_error",
                        "model repeated an explicit fake tool call after correction",
                    )
                return "hallucination_fix"

        if isinstance(last, HumanMessage):
            content = (
                last.content if isinstance(last.content, str) else str(last.content)
            )
            if content.startswith("🪞"):
                return "call_llm"
        return "__end__"

    return route