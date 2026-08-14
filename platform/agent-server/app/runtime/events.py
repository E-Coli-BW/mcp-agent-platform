"""Transport-neutral events emitted by the platform run coordinator."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, TypeAlias

RuntimeEventKind: TypeAlias = Literal[
    "approval_required",
    "child_event",
    "content",
    "done",
    "keepalive",
    "status",
    "tool_end",
    "tool_start",
]


@dataclass(frozen=True, slots=True)
class RuntimeStreamEvent:
    """One protocol-independent event visible to a runtime client."""

    kind: RuntimeEventKind
    payload: dict[str, Any] = field(default_factory=dict)
    event_id: str | None = None