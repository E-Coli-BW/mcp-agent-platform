"""Tests for the @tool decorator."""

from agent_sdk.tool import tool


def test_tool_decorator_sets_metadata() -> None:
    """Decorator should attach tool metadata to the wrapped function."""

    @tool(
        name="ticket_create",
        description="Create a ticket",
        permissions=["tickets:write"],
    )
    def ticket_create(summary: str) -> str:
        return summary

    metadata = ticket_create._tool_metadata
    assert metadata.name == "ticket_create"
    assert metadata.description == "Create a ticket"
    assert metadata.permissions == ["tickets:write"]


def test_tool_function_still_callable() -> None:
    """Decorated functions should still be callable."""

    @tool(name="echo", description="Echo a value")
    def echo(value: str) -> str:
        return value

    assert echo("hello") == "hello"
