"""Tool invocation envelope helpers.

This is Phase 3's contract-first slice: compute stable metadata for tool calls
without changing execution behavior. Later phases can move this envelope in
front of execution for idempotency, approvals, and policy enforcement.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import Enum
from typing import Any


class ToolRisk(str, Enum):
    READ = "read"
    WRITE = "write"
    EXECUTE = "execute"
    META = "meta"
    UNKNOWN = "unknown"


READ_TOOLS = {
    "file_read",
    "file_list",
    "file_search",
    "git_status",
    "git_diff",
    "git_branch",
    "git_log",
    "memory_search",
    "memory_context",
    "memory_get",
    "skill_get",
    "rag_search",
    "ticket_list",
}

WRITE_TOOLS = {
    "file_write",
    "file_edit",
    "git_commit",
    "memory_set",
    "ticket_create",
    "ticket_update",
    "pr_create",
}

EXECUTE_TOOLS = {
    "code_run",
    "code_shell",
    "run_tests",
}

META_TOOLS = {
    "spawn_subagent",
}


def classify_tool_risk(tool_name: str) -> ToolRisk:
    if tool_name in READ_TOOLS:
        return ToolRisk.READ
    if tool_name in WRITE_TOOLS:
        return ToolRisk.WRITE
    if tool_name in EXECUTE_TOOLS:
        return ToolRisk.EXECUTE
    if tool_name in META_TOOLS:
        return ToolRisk.META
    return ToolRisk.UNKNOWN


def normalize_arguments(arguments: dict[str, Any]) -> str:
    """Return deterministic JSON for hashing and replay comparison."""
    return json.dumps(
        arguments,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )


def hash_arguments(arguments: dict[str, Any]) -> str:
    normalized = normalize_arguments(arguments)
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def make_idempotency_key(
    *,
    run_id: str,
    step_seq: int,
    tool_name: str,
    args_hash: str | None = None,
) -> str:
    # Deliberately exclude args_hash: the key identifies the workflow step,
    # while args_hash is stored separately to detect same-key/different-args
    # conflicts on replay.
    raw = f"{run_id}:{step_seq}:{tool_name}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return f"tool:{digest}"


@dataclass(frozen=True)
class ToolInvocationEnvelope:
    invocation_id: str
    run_id: str
    request_id: str
    session_id: str
    tenant_id: str
    step_seq: int
    tool_name: str
    args_hash: str
    idempotency_key: str
    risk: ToolRisk

    def to_event_payload(self) -> dict[str, Any]:
        return {
            "invocation_id": self.invocation_id,
            "run_id": self.run_id,
            "request_id": self.request_id,
            "session_id": self.session_id,
            "tenant_id": self.tenant_id,
            "step_seq": self.step_seq,
            "tool_name": self.tool_name,
            "args_hash": self.args_hash,
            "idempotency_key": self.idempotency_key,
            "risk": self.risk.value,
        }


def make_tool_invocation_envelope(
    *,
    run_id: str,
    request_id: str,
    session_id: str,
    tenant_id: str,
    step_seq: int,
    tool_name: str,
    arguments: dict[str, Any],
) -> ToolInvocationEnvelope:
    args_hash = hash_arguments(arguments)
    invocation_id = f"tinv-{run_id}-{step_seq}"
    return ToolInvocationEnvelope(
        invocation_id=invocation_id,
        run_id=run_id,
        request_id=request_id,
        session_id=session_id,
        tenant_id=tenant_id,
        step_seq=step_seq,
        tool_name=tool_name,
        args_hash=args_hash,
        idempotency_key=make_idempotency_key(
            run_id=run_id,
            step_seq=step_seq,
            tool_name=tool_name,
            args_hash=args_hash,
        ),
        risk=classify_tool_risk(tool_name),
    )
