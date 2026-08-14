"""Interpret LangChain events into visible Agent output state."""

from __future__ import annotations

from typing import Any


class ToolEventTracker:
    """Expose one lifecycle for each outer tool run, hiding nested wrappers."""

    def __init__(self) -> None:
        self._next_seq = 0
        self._visible_runs: dict[str, int] = {}

    @property
    def count(self) -> int:
        return self._next_seq

    def start(self, event: dict[str, Any]) -> int | None:
        run_id = str(event.get("run_id", ""))
        parent_ids = {str(parent_id) for parent_id in event.get("parent_ids", [])}
        if any(parent_id in self._visible_runs for parent_id in parent_ids):
            return None
        self._next_seq += 1
        if run_id:
            self._visible_runs[run_id] = self._next_seq
        return self._next_seq

    def end(self, event: dict[str, Any]) -> int | None:
        run_id = str(event.get("run_id", ""))
        if not run_id:
            return self._next_seq or None
        return self._visible_runs.pop(run_id, None)


class FinalResponseBuffer:
    """Retain only the latest actor-model response candidate."""

    def __init__(self) -> None:
        self._parts: list[str] = []

    def start_call(self) -> None:
        self._parts = []

    def append(self, content: str) -> None:
        self._parts.append(content)

    @property
    def text(self) -> str:
        return "".join(self._parts)


def is_actor_model_event(event: dict[str, Any]) -> bool:
    node = event.get("metadata", {}).get("langgraph_node")
    return node in (None, "call_llm")


def verified_artifact_answer_from_chain_event(
    event: dict[str, Any],
) -> str | None:
    """Return a rendered plugin artifact from a graph node completion event."""
    if event.get("event") != "on_chain_end":
        return None
    output = event.get("data", {}).get("output")
    messages = (
        output.get("messages", [])
        if isinstance(output, dict)
        else getattr(output, "messages", [])
    )
    if not isinstance(messages, (list, tuple)):
        messages = [messages]
    for message in reversed(messages):
        additional_kwargs = getattr(message, "additional_kwargs", {})
        if isinstance(additional_kwargs.get("verified_artifact"), dict):
            content = getattr(message, "content", "")
            return content if isinstance(content, str) else str(content)
    return None