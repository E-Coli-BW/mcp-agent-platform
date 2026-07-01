"""@tool decorator for plugin tools."""

import functools
from dataclasses import dataclass, field


@dataclass
class ToolMetadata:
    """Metadata attached to a decorated tool function."""

    name: str
    description: str
    permissions: list[str] = field(default_factory=list)
    config_schema: dict | None = None


def tool(name: str, description: str, permissions: list[str] | None = None):
    """Decorator to mark a function as an agent tool.

    Usage:
        @tool(name="ticket_create", description="Create a Jira ticket",
              permissions=["tickets:write"])
        def ticket_create(ctx: ToolContext, summary: str, priority: str = "medium") -> str:
            ...
    """

    def decorator(func):
        func._tool_metadata = ToolMetadata(
            name=name,
            description=description,
            permissions=permissions or [],
        )

        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            return func(*args, **kwargs)

        wrapper._tool_metadata = func._tool_metadata
        return wrapper

    return decorator
