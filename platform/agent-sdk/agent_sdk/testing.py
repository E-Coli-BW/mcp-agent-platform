"""Test harness for unit testing plugin tools without running the server."""

from unittest.mock import MagicMock

from agent_sdk.context import ToolContext
from agent_sdk.knowledge import KnowledgeBaseClient
from agent_sdk.memory import MemoryClient


class ToolTestHarness:
    """Creates a mock ToolContext for testing plugin tools in isolation."""

    def __init__(
        self,
        tenant_id: str = "test-tenant",
        session_id: str = "test-session",
        user_role: str = "USER",
        secrets: dict[str, str] | None = None,
        workspace: str = "/tmp/test",
    ):
        self.context = ToolContext(
            tenant_id=tenant_id,
            session_id=session_id,
            user_role=user_role,
            memory=MagicMock(spec=MemoryClient),
            knowledge=MagicMock(spec=KnowledgeBaseClient),
            secrets=secrets or {},
            workspace=workspace,
        )
