"""Usage accounting observer."""

from __future__ import annotations

from typing import Any

from app.runtime.observers.base import (
    RuntimeObservation,
    TurnObservationReport,
    TurnObserverContext,
)
from app.usage import RequestUsage, UsageTracker, estimate_tokens, get_usage_tracker


def prompt_tokens_from_event(event: dict[str, Any] | None) -> int:
    if event is None:
        return 0
    model_input = event.get("data", {}).get("input", {})
    messages = model_input.get("messages", []) if isinstance(model_input, dict) else []
    if messages and isinstance(messages[0], list):
        messages = messages[0]
    total = 0
    for message in messages:
        content = getattr(message, "content", None)
        if isinstance(content, str):
            total += estimate_tokens(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    total += estimate_tokens(part["text"])
    return total


def completion_tokens_from_event(event: dict[str, Any] | None) -> int:
    if event is None:
        return 0
    chunk = event.get("data", {}).get("chunk")
    content = getattr(chunk, "content", None)
    return estimate_tokens(content) if isinstance(content, str) and content else 0


class UsageObserver:
    def __init__(
        self,
        context: TurnObserverContext,
        report: TurnObservationReport,
        tracker: UsageTracker | None = None,
    ) -> None:
        self._context = context
        self._report = report
        self._tracker = tracker or get_usage_tracker()

    async def observe(self, observation: RuntimeObservation) -> None:
        if observation.kind == "model_started":
            self._report.prompt_tokens += prompt_tokens_from_event(
                observation.raw_event
            )
        elif observation.kind == "model_streamed":
            self._report.completion_tokens += completion_tokens_from_event(
                observation.raw_event
            )
        elif observation.kind == "tool_started":
            self._report.tool_count = max(
                self._report.tool_count,
                int(observation.payload["seq"]),
            )
        elif observation.kind == "turn_finished":
            self._report.duration_ms = int(observation.payload["duration_ms"])
            self._tracker.record(
                RequestUsage(
                    model=self._context.model,
                    prompt_tokens=self._report.prompt_tokens,
                    completion_tokens=self._report.completion_tokens,
                    tool_calls=self._report.tool_count,
                    duration_ms=self._report.duration_ms,
                )
            )