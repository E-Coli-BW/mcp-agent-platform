"""Small append-only feedback store used by local evaluation and dashboard."""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

BASE_DIR = Path.home() / ".mcp-local"
FEEDBACK_FILE = BASE_DIR / "user-feedback.jsonl"
TRACE_FILE = BASE_DIR / "retrieval-traces.jsonl"


def _append(path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
    return payload


def record_user_feedback(**fields: Any) -> dict[str, Any]:
    return _append(FEEDBACK_FILE, {
        "feedback_id": f"feedback-{uuid.uuid4().hex[:12]}",
        "created_at": time.time(),
        **fields,
    })


def record_retrieval_trace(**fields: Any) -> dict[str, Any]:
    return _append(TRACE_FILE, {
        "trace_id": f"retrieval-{uuid.uuid4().hex[:12]}",
        "created_at": time.time(),
        **fields,
    })
