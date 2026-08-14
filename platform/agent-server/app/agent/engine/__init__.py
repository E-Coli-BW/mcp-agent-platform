"""Agent Engine composition seam."""

from app.agent.engine.contracts import (
    AgentEngine,
    AgentEvent,
    AgentExecutionResult,
    AgentResumeRequest,
    AgentTurnRequest,
)

_engine: AgentEngine | None = None


def get_agent_engine() -> AgentEngine:
    """Return the process-wide Agent Engine implementation."""
    global _engine
    if _engine is None:
        from app.agent.engine.langgraph import LangGraphAgentEngine

        _engine = LangGraphAgentEngine()
    return _engine


__all__ = [
    "AgentEngine",
    "AgentEvent",
    "AgentExecutionResult",
    "AgentResumeRequest",
    "AgentTurnRequest",
    "get_agent_engine",
]