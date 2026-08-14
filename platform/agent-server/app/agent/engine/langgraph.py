"""LangGraph adapter for the Agent Engine contract."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, cast

from langchain_core.messages import HumanMessage

from app.agent.engine.contracts import (
    AgentEvent,
    AgentExecutionResult,
    AgentResumeRequest,
    AgentTurnRequest,
)
from app.agent.engine.registry import LangGraphAgentRegistry, get_agent_registry

AgentResolver = Callable[..., Awaitable[Any]]


class LangGraphAgentEngine:
    """Expose existing LangGraph agents through the stable Engine boundary."""

    def __init__(
        self,
        agent_resolver: AgentResolver | None = None,
        registry: LangGraphAgentRegistry | None = None,
    ) -> None:
        self._agent_resolver = agent_resolver
        self._registry = registry or get_agent_registry()

    async def stream_turn(
        self,
        request: AgentTurnRequest,
    ) -> AsyncIterator[AgentEvent]:
        agent = await self._resolve_agent(request)
        return cast(
            AsyncIterator[AgentEvent],
            agent.astream_events(
                {"messages": [HumanMessage(content=request.user_input)]},
                config=self._config(request.session_id, request.recursion_limit),
                version="v2",
            ),
        )

    async def pending_nodes(self, request: AgentResumeRequest) -> tuple[str, ...]:
        agent = await self._resolve_agent(request)
        snapshot = await agent.aget_state(
            self._config(request.session_id, request.recursion_limit)
        )
        return tuple(snapshot.next)

    async def resume(self, request: AgentResumeRequest) -> AgentExecutionResult:
        agent = await self._resolve_agent(request)
        result = await agent.ainvoke(
            None,
            self._config(request.session_id, request.recursion_limit),
        )
        return cast(AgentExecutionResult, result)

    async def _resolve_agent(
        self,
        request: AgentTurnRequest | AgentResumeRequest,
    ) -> Any:
        resolver = self._agent_resolver
        if resolver is None:
            resolver = self._registry.get_agent
        return await resolver(
            request.model,
            temperature=request.temperature,
            tenant_id=request.tenant_id,
            session_id=request.session_id,
            prompt_version=request.prompt_version,
        )

    @staticmethod
    def _config(session_id: str, recursion_limit: int) -> dict[str, Any]:
        return {
            "configurable": {"thread_id": session_id},
            "recursion_limit": recursion_limit,
        }