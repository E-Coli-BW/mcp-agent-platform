"""Stable execution contract between the Agent core and platform runtime."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Any, Protocol, TypeAlias

AgentEvent: TypeAlias = dict[str, Any]
AgentExecutionResult: TypeAlias = Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class AgentTurnRequest:
    """Inputs required by the Agent core to execute one user turn."""

    model: str
    session_id: str
    tenant_id: str
    user_input: str
    recursion_limit: int
    temperature: float | None = None
    prompt_version: str | None = None


@dataclass(frozen=True, slots=True)
class AgentResumeRequest:
    """Inputs required to inspect or resume a checkpointed Agent graph."""

    model: str
    session_id: str
    tenant_id: str
    recursion_limit: int
    temperature: float | None = None
    prompt_version: str | None = None


class AgentEngine(Protocol):
    """Execution boundary consumed by platform runtime capabilities."""

    async def stream_turn(
        self,
        request: AgentTurnRequest,
    ) -> AsyncIterator[AgentEvent]: ...

    async def pending_nodes(self, request: AgentResumeRequest) -> tuple[str, ...]: ...

    async def resume(self, request: AgentResumeRequest) -> AgentExecutionResult: ...