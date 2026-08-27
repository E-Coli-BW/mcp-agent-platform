"""Interpret LangChain events into visible Agent output state."""

from __future__ import annotations

import re
from typing import Any

_SAFE_PROGRESS_PREFIX = re.compile(
    r"^(?:(?:I(?:'m| am| will|'ll)|Now)\s+)?"
    r"(?:read(?:ing)?|search(?:ing)?|inspect(?:ing)?|check(?:ing)?|"
    r"run(?:ning)?|edit(?:ing)?|updat(?:e|ing)|writ(?:e|ing)|"
    r"test(?:ing)?|review(?:ing)?|open(?:ing)?|list(?:ing)?|"
    r"compar(?:e|ing)|validat(?:e|ing))\b",
    re.IGNORECASE,
)
_UNSAFE_PROGRESS_TEXT = re.compile(
    r"\b(?:analysis|because|chain[- ]of[- ]thought|I believe|I think|"
    r"reasoning|therefore|thus)\b",
    re.IGNORECASE,
)


class ResponseBufferLimitError(RuntimeError):
    """The provider streamed more final-answer text than the runtime permits."""

    terminal_reason = "response_buffer_limit"
    retryable = False


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

    def __init__(self, max_chars: int = 64_000) -> None:
        if max_chars < 1:
            raise ValueError("max_chars must be positive")
        self._max_chars = max_chars
        self._char_count = 0
        self._parts: list[str] = []

    def start_call(self) -> None:
        self._char_count = 0
        self._parts = []

    def append(self, content: str) -> None:
        if self._char_count + len(content) > self._max_chars:
            raise ResponseBufferLimitError(
                f"final response exceeded {self._max_chars} characters"
            )
        self._parts.append(content)
        self._char_count += len(content)

    @property
    def text(self) -> str:
        return "".join(self._parts)


def is_actor_model_event(event: dict[str, Any]) -> bool:
    node = event.get("metadata", {}).get("langgraph_node")
    return node in (None, "call_llm")


def _message_with_tool_calls(value: Any, depth: int = 0) -> Any | None:
    if value is None or depth > 4:
        return None
    if getattr(value, "tool_calls", None):
        return value
    if isinstance(value, dict):
        children = value.values()
    elif isinstance(value, (list, tuple)):
        children = value
    else:
        children = (
            getattr(value, name, None)
            for name in ("message", "messages", "generations", "output")
        )
    for child in children:
        message = _message_with_tool_calls(child, depth + 1)
        if message is not None:
            return message
    return None


def tool_call_progress_from_model_event(
    event: dict[str, Any],
    max_chars: int = 240,
) -> str | None:
    """Return bounded non-reasoning text attached to a tool-calling model turn."""
    if event.get("event") != "on_chat_model_end":
        return None
    output = event.get("data", {}).get("output")
    message = _message_with_tool_calls(output)
    if message is None:
        return None
    content = getattr(message, "content", "")
    if not isinstance(content, str) or not content.strip():
        return None
    if re.search(r"<\s*(?:think|thinking|reasoning)\b", content, re.IGNORECASE):
        return None
    normalized = " ".join(content.split())
    sentence_ends = re.findall(r"[.!?](?:\s|$)", normalized)
    if (
        len(normalized) > max_chars
        or len(sentence_ends) > 1
        or _UNSAFE_PROGRESS_TEXT.search(normalized)
        or not _SAFE_PROGRESS_PREFIX.match(normalized)
    ):
        return None
    return normalized


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