"""OpenAI-compatible chunks and named SSE event helpers."""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

from app.runtime.agent_output import (
    FinalResponseBuffer,
    ToolEventTracker,
    is_actor_model_event,
    verified_artifact_answer_from_chain_event,
)
from app.runtime.events import RuntimeStreamEvent

__all__ = [
    "FinalResponseBuffer",
    "ToolEventTracker",
    "is_actor_model_event",
    "make_child_event",
    "make_chunk",
    "make_status_event",
    "make_tool_event",
    "project_runtime_event",
    "verified_artifact_answer_from_chain_event",
]


def make_chunk(content: str, model: str, finish_reason: str | None = None) -> str:
    """Format a single OpenAI-compatible content chunk."""
    chunk = {
        "id": f"chatcmpl-{uuid.uuid4().hex[:8]}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": {"content": content} if content else {},
                "finish_reason": finish_reason,
            }
        ],
    }
    return json.dumps(chunk)


def make_tool_event(
    action: str,
    tool_name: str,
    data: dict[str, Any],
    seq: int,
    event_id: str | None = None,
) -> dict[str, str]:
    """Create the canonical named SSE tool event."""
    event = {
        "event": f"tool_{action}",
        "data": json.dumps({"tool": tool_name, "seq": seq, **data}),
    }
    if event_id:
        event["id"] = event_id
    return event


def make_status_event(
    state: str,
    event_id: str | None = None,
    **extra: Any,
) -> dict[str, str]:
    """Create a named SSE status event."""
    event = {"event": "status", "data": json.dumps({"state": state, **extra})}
    if event_id:
        event["id"] = event_id
    return event


def make_child_event(bus_event: dict[str, Any]) -> dict[str, str]:
    """Convert a fleet-bus event into a named browser SSE event."""
    return {
        "event": bus_event["type"],
        "data": json.dumps(
            {
                "child_session_id": bus_event.get("child_session_id"),
                "role": bus_event.get("role"),
                **{
                    key: value
                    for key, value in bus_event.items()
                    if key not in ("type", "root_session_id", "child_session_id", "role")
                },
            }
        ),
    }


def project_runtime_event(event: RuntimeStreamEvent) -> str | dict[str, str]:
    """Project one transport-neutral runtime event onto the OpenAI SSE wire."""
    payload = event.payload
    if event.kind == "status":
        return make_status_event(
            str(payload["state"]),
            event_id=event.event_id,
            **{key: value for key, value in payload.items() if key != "state"},
        )
    if event.kind in ("tool_start", "tool_end"):
        return make_tool_event(
            event.kind.removeprefix("tool_"),
            str(payload["tool"]),
            dict(payload["data"]),
            seq=int(payload["seq"]),
            event_id=event.event_id,
        )
    if event.kind == "approval_required":
        return {
            "event": "approval_required",
            "data": json.dumps(payload["approval"].to_dict()),
        }
    if event.kind == "child_event":
        return make_child_event(dict(payload["event"]))
    if event.kind == "content":
        return make_chunk(
            str(payload.get("content", "")),
            str(payload["model"]),
            finish_reason=payload.get("finish_reason"),
        )
    if event.kind == "keepalive":
        return ": keepalive\n"
    if event.kind == "done":
        return "[DONE]"
    raise ValueError(f"unsupported runtime event kind: {event.kind}")