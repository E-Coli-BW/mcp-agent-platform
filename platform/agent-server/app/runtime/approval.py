"""Runtime approval control-plane state for high-risk tool requests."""

from __future__ import annotations

import asyncio
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path


@dataclass
class ApprovalRequest:
    approval_id: str
    run_id: str
    tenant_id: str
    tool_name: str
    reason: str
    status: str = "pending"
    created_at: float = 0.0
    decided_at: float | None = None
    decided_by: str | None = None

    def to_dict(self) -> dict:
        return self.__dict__.copy()


class ApprovalRegistry:
    def __init__(self, database_path: str | None = None) -> None:
        self._requests: dict[str, ApprovalRequest] = {}
        self._events: dict[str, asyncio.Event] = {}
        self._lock = asyncio.Lock()
        self._database_path = Path(database_path) if database_path else None
        if self._database_path:
            self._database_path.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(self._database_path) as conn:
                conn.execute("""CREATE TABLE IF NOT EXISTS agent_approvals (
                    approval_id TEXT PRIMARY KEY, run_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL, tool_name TEXT NOT NULL,
                    reason TEXT NOT NULL, status TEXT NOT NULL,
                    created_at REAL NOT NULL, decided_at REAL,
                    decided_by TEXT
                )""")

    def _persist(self, request: ApprovalRequest) -> None:
        if not self._database_path:
            return
        with sqlite3.connect(self._database_path) as conn:
            conn.execute(
                """INSERT OR REPLACE INTO agent_approvals
                (approval_id, run_id, tenant_id, tool_name, reason, status,
                 created_at, decided_at, decided_by)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (request.approval_id, request.run_id, request.tenant_id,
                 request.tool_name, request.reason, request.status,
                 request.created_at, request.decided_at, request.decided_by),
            )

    async def create(self, *, run_id: str, tenant_id: str, tool_name: str, reason: str) -> ApprovalRequest:
        async with self._lock:
            request = ApprovalRequest(
                approval_id=f"approval-{uuid.uuid4().hex[:12]}",
                run_id=run_id,
                tenant_id=tenant_id,
                tool_name=tool_name,
                reason=reason,
                created_at=time.time(),
            )
            self._requests[request.approval_id] = request
            self._events[request.approval_id] = asyncio.Event()
            self._persist(request)
            return request

    async def get(self, approval_id: str) -> ApprovalRequest | None:
        async with self._lock:
            request = self._requests.get(approval_id)
            if request is not None or not self._database_path:
                return request
            with sqlite3.connect(self._database_path) as conn:
                row = conn.execute(
                    "SELECT approval_id, run_id, tenant_id, tool_name, reason, status, created_at, decided_at, decided_by FROM agent_approvals WHERE approval_id = ?",
                    (approval_id,),
                ).fetchone()
            if row is None:
                return None
            request = ApprovalRequest(*row)
            self._requests[approval_id] = request
            self._events.setdefault(approval_id, asyncio.Event())
            return request

    async def decide(self, approval_id: str, *, status: str, decided_by: str) -> ApprovalRequest | None:
        if status not in {"approved", "rejected"}:
            raise ValueError("status must be approved or rejected")
        async with self._lock:
            request = self._requests.get(approval_id)
            if request is None:
                return None
            if request.status != "pending":
                return request
            request.status = status
            request.decided_at = time.time()
            request.decided_by = decided_by
            self._events[approval_id].set()
            self._persist(request)
            return request

    async def wait_for_decision(self, approval_id: str, timeout: float = 900) -> ApprovalRequest | None:
        event = self._events.get(approval_id)
        if event is None:
            return None
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            async with self._lock:
                request = self._requests.get(approval_id)
                if request is not None and request.status == "pending":
                    request.status = "expired"
                    request.decided_at = time.time()
            return await self.get(approval_id)
        return await self.get(approval_id)

    async def clear(self) -> None:
        async with self._lock:
            self._requests.clear()


def _build_registry() -> ApprovalRegistry:
    from app.config import settings
    if settings.runtime_store_backend.strip().lower() == "sqlite":
        return ApprovalRegistry(settings.runtime_store_sqlite_path)
    return ApprovalRegistry()


_registry = _build_registry()


def get_approval_registry() -> ApprovalRegistry:
    return _registry


def reset_approval_registry_for_tests() -> None:
    global _registry
    _registry = ApprovalRegistry()
