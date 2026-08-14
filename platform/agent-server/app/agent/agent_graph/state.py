"""Typed state and terminal errors for the v2 Agent graph."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Annotated

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages
from pydantic import BaseModel, Field

from app.agent.compressor import InvestigationState


class AgentState(BaseModel):
    """Checkpointed Agent state with request counters and durable facts."""

    messages: Annotated[Sequence[BaseMessage], add_messages] = Field(
        default_factory=list
    )
    workspace_context: str = ""
    skills_catalog: str = ""
    loop_counter: int = 0
    consecutive_errors: int = 0
    critique_attempts: int = 0
    investigation: InvestigationState = Field(default_factory=InvestigationState)
    compression_summary: str = ""


@dataclass(frozen=True)
class LlmMessageView:
    messages: list[BaseMessage]
    compression_summary: str
    overflow: bool
    total_chars: int
    budget_chars: int


class ContextBudgetExceededError(RuntimeError):
    """The required prompt floor cannot fit the configured model budget."""


class AgentTerminationError(RuntimeError):
    """A bounded loop ended without a valid completed assistant response."""

    retryable = False

    def __init__(self, terminal_reason: str, message: str):
        self.terminal_reason = terminal_reason
        super().__init__(message)