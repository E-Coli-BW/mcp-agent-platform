"""Context object injected into every tool call."""

from dataclasses import dataclass

from agent_sdk.knowledge import KnowledgeBaseClient
from agent_sdk.memory import MemoryClient


@dataclass
class ToolContext:
    """Context provided to every plugin tool invocation."""

    tenant_id: str
    session_id: str
    user_role: str
    memory: MemoryClient
    knowledge: KnowledgeBaseClient
    secrets: dict[str, str]
    workspace: str
