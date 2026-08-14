"""Build v2 Agent graphs from model names or declarative Agent configs."""

from __future__ import annotations

from app.agent.agent_graph.builder import build_agent_graph
from app.agent.checkpointing import get_checkpointer
from app.agent.model.factory import create_chat_model
from app.agent.prompts import get_system_prompt
from app.agent.runtime_profile import resolve_runtime_profile
from app.config import settings
from app.context.request_context import set_agent_contract
from app.registry.tool_registry import resolve_tools


def create_agent_v2(
    model_name: str | None = None,
    temperature: float | None = None,
):
    """Create a v2 Agent from the default tool registry."""
    from app.tools.definitions import get_tools

    return build_agent_graph(
        model=create_chat_model(model_name, temperature=temperature),
        tools=get_tools(),
        system_prompt=get_system_prompt(),
        checkpointer=get_checkpointer(),
    )


def create_agent_v2_from_config(config, temperature: float | None = None):
    """Create a v2 Agent from a declarative AgentConfig."""
    set_agent_contract(config.id, config.knowledge.get("collection", ""))
    return build_agent_graph(
        model=create_chat_model(config.model, temperature=temperature),
        tools=resolve_tools(config.tools),
        system_prompt=config.prompt or get_system_prompt(),
        checkpointer=get_checkpointer(),
        max_steps=resolve_runtime_profile(config, settings).max_tool_calls,
    )