"""Topology assembly for the v2 Agent graph."""

from __future__ import annotations

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.tools import BaseTool
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

from app.agent.agent_graph.nodes.context import (
    inject_context_node,
    reset_turn_state_node,
    update_investigation_node,
)
from app.agent.agent_graph.nodes.intent_router import (
    make_intent_router_node,
    route_after_intent_router,
)
from app.agent.agent_graph.nodes.model import make_call_llm_node
from app.agent.agent_graph.nodes.reflexion import make_maybe_critique_node
from app.agent.agent_graph.nodes.tools import (
    _format_tool_error,
    _truncate_tool_output,
    make_error_tracking_node,
)
from app.agent.agent_graph.nodes.verification import (
    make_hallucination_fix_node,
    make_synthesize_artifact_node,
)
from app.agent.agent_graph.routing import (
    make_route_node,
    route_after_artifact_synthesis,
)
from app.agent.agent_graph.state import AgentState
from app.config import settings
from app.runtime.tool_authorization import wrap_tool_with_authorization


def build_agent_graph(
    model: BaseChatModel,
    tools: list[BaseTool],
    system_prompt: str,
    checkpointer=None,
    max_steps: int | None = None,
    max_context_chars: int | None = None,
    direct_tool_routing_enabled: bool | None = None,
):
    """Build and compile the explicit v2 ReAct topology."""
    resolved_max_steps = max_steps or settings.max_agent_steps
    truncated_tools = [
        _truncate_tool_output(tool, settings.max_tool_output) for tool in tools
    ]
    execution_tools = [
        wrap_tool_with_authorization(tool) for tool in truncated_tools
    ]
    available_tools = {tool.name for tool in execution_tools}

    graph = StateGraph(AgentState)
    graph.add_node("reset_turn_state", reset_turn_state_node)
    graph.add_node("inject_context", inject_context_node)
    graph.add_node(
        "intent_router",
        make_intent_router_node(
            execution_tools,
            enabled=direct_tool_routing_enabled,
        ),
    )
    graph.add_node(
        "call_llm",
        make_call_llm_node(
            model,
            execution_tools,
            system_prompt,
            max_context_chars=max_context_chars,
            max_tool_calls=resolved_max_steps,
        ),
    )
    graph.add_node("maybe_critique", make_maybe_critique_node())
    graph.add_node(
        "tools",
        ToolNode(execution_tools, handle_tool_errors=_format_tool_error),
    )
    graph.add_node("track_errors", make_error_tracking_node())
    graph.add_node("update_investigation_initial", update_investigation_node)
    graph.add_node("update_investigation_tools", update_investigation_node)
    graph.add_node("synthesize_artifact", make_synthesize_artifact_node())
    graph.add_node(
        "hallucination_fix",
        make_hallucination_fix_node(available_tools),
    )

    graph.add_edge(START, "reset_turn_state")
    graph.add_edge("reset_turn_state", "inject_context")
    graph.add_edge("inject_context", "update_investigation_initial")
    graph.add_edge("update_investigation_initial", "intent_router")
    graph.add_conditional_edges(
        "intent_router",
        route_after_intent_router,
        {"tools": "tools", "call_llm": "call_llm"},
    )
    graph.add_edge("call_llm", "maybe_critique")
    graph.add_conditional_edges(
        "maybe_critique",
        make_route_node(resolved_max_steps, available_tools),
    )
    graph.add_edge("hallucination_fix", "call_llm")
    graph.add_edge("tools", "track_errors")
    graph.add_edge("track_errors", "update_investigation_tools")
    graph.add_edge("update_investigation_tools", "synthesize_artifact")
    graph.add_conditional_edges(
        "synthesize_artifact",
        route_after_artifact_synthesis,
        {"call_llm": "call_llm", "__end__": END},
    )
    return graph.compile(checkpointer=checkpointer)