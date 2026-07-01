"""Example plugin tool — hello world."""

from agent_sdk import ToolContext, tool


@tool(name="hello_world", description="A simple hello world tool for testing")
def hello_world(ctx: ToolContext, name: str = "World") -> str:
    """Greet someone by name."""
    return f"👋 Hello, {name}! (tenant: {ctx.tenant_id})"
