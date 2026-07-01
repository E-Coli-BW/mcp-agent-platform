"""Tests for ToolContext."""

from agent_sdk.context import ToolContext
from agent_sdk.knowledge import KnowledgeBaseClient
from agent_sdk.memory import MemoryClient


def test_context_has_all_fields() -> None:
    """ToolContext should expose all expected fields."""

    memory = MemoryClient(base_url="https://memory.example.com", auth_token="token")
    knowledge = KnowledgeBaseClient()

    context = ToolContext(
        tenant_id="tenant-123",
        session_id="session-456",
        user_role="ADMIN",
        memory=memory,
        knowledge=knowledge,
        secrets={"API_KEY": "secret"},
        workspace="/workspace/project",
    )

    assert context.tenant_id == "tenant-123"
    assert context.session_id == "session-456"
    assert context.user_role == "ADMIN"
    assert context.memory is memory
    assert context.knowledge is knowledge
    assert context.secrets == {"API_KEY": "secret"}
    assert context.workspace == "/workspace/project"
