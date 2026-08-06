"""Lightweight runtime event recorder.

This module is the first concrete slice of the runtime refactor roadmap:
record a replayable run timeline without changing LangGraph execution, SSE
formatting, storage, or tool behavior.

The current implementation is process-local and bounded. It is intentionally
not a durability claim; it gives us a stable contract and tests before we add
a persistent store.
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol


class RunStatus(str, Enum):
    """Coarse run lifecycle state."""

    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class RunEvent:
    """One ordered event in a run timeline."""

    run_id: str
    event_seq: int
    event_type: str
    timestamp: float
    payload: dict[str, Any] = field(default_factory=dict)

    @property
    def event_id(self) -> str:
        """Stable cursor shape for future SSE replay."""
        return f"{self.run_id}:{self.event_seq}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "run_id": self.run_id,
            "event_seq": self.event_seq,
            "event_type": self.event_type,
            "timestamp": self.timestamp,
            "payload": dict(self.payload),
        }


@dataclass
class _RunTimeline:
    status: RunStatus
    request_id: str
    tenant_id: str
    session_id: str
    model: str
    created_at: float
    updated_at: float
    events: list[RunEvent] = field(default_factory=list)


class RunStore(Protocol):
    """Storage boundary used by store-backed recorders."""

    async def start_run(
        self,
        *,
        run_id: str,
        request_id: str,
        tenant_id: str,
        session_id: str,
        model: str,
        metadata: dict[str, Any] | None = None,
    ) -> RunEvent: ...

    async def append_event(
        self,
        run_id: str,
        event_type: str,
        payload: dict[str, Any] | None = None,
    ) -> RunEvent: ...

    async def finish_run(
        self,
        run_id: str,
        status: RunStatus,
        payload: dict[str, Any] | None = None,
    ) -> RunEvent: ...

    async def list_events(self, run_id: str, after_seq: int = 0) -> list[RunEvent]: ...

    async def get_status(self, run_id: str) -> RunStatus | None: ...

    async def get_metadata(self, run_id: str) -> dict[str, Any] | None: ...

    async def clear(self) -> None: ...


class InMemoryRunRecorder:
    """Bounded process-local run recorder.

    The lock keeps event sequence allocation deterministic under async
    interleaving. It is not distributed and should be replaced by a persistent
    recorder in later roadmap phases.
    """

    def __init__(self, max_runs: int = 500):
        self._runs: OrderedDict[str, _RunTimeline] = OrderedDict()
        self._max_runs = max_runs
        self._lock = asyncio.Lock()

    async def start_run(
        self,
        *,
        run_id: str,
        request_id: str,
        tenant_id: str,
        session_id: str,
        model: str,
        metadata: dict[str, Any] | None = None,
    ) -> RunEvent:
        """Create a run timeline if needed and emit ``run_started``.

        Repeated calls for an existing run are treated as idempotent attach
        operations: the original timeline is kept and no duplicate start event
        is emitted.
        """
        async with self._lock:
            if run_id in self._runs:
                self._runs.move_to_end(run_id)
                events = self._runs[run_id].events
                if events:
                    return events[0]

            now = time.time()
            self._runs[run_id] = _RunTimeline(
                status=RunStatus.RUNNING,
                request_id=request_id,
                tenant_id=tenant_id,
                session_id=session_id,
                model=model,
                created_at=now,
                updated_at=now,
            )
            self._evict_if_needed()
            return self._append_locked(
                run_id,
                "run_started",
                {
                    "request_id": request_id,
                    "tenant_id": tenant_id,
                    "session_id": session_id,
                    "model": model,
                    **(metadata or {}),
                },
            )

    async def record_event(
        self,
        run_id: str,
        event_type: str,
        payload: dict[str, Any] | None = None,
    ) -> RunEvent:
        async with self._lock:
            if run_id not in self._runs:
                now = time.time()
                self._runs[run_id] = _RunTimeline(
                    status=RunStatus.RUNNING,
                    request_id="",
                    tenant_id="",
                    session_id="",
                    model="",
                    created_at=now,
                    updated_at=now,
                )
                self._evict_if_needed()
            return self._append_locked(run_id, event_type, payload or {})

    async def finish_run(
        self,
        run_id: str,
        status: RunStatus,
        payload: dict[str, Any] | None = None,
    ) -> RunEvent:
        event_type = {
            RunStatus.COMPLETED: "run_completed",
            RunStatus.FAILED: "run_failed",
            RunStatus.CANCELLED: "run_cancelled",
            RunStatus.RUNNING: "run_updated",
        }[status]
        async with self._lock:
            if run_id not in self._runs:
                now = time.time()
                self._runs[run_id] = _RunTimeline(
                    status=status,
                    request_id="",
                    tenant_id="",
                    session_id="",
                    model="",
                    created_at=now,
                    updated_at=now,
                )
                self._evict_if_needed()
            self._runs[run_id].status = status
            return self._append_locked(run_id, event_type, payload or {})

    async def list_events(self, run_id: str, after_seq: int = 0) -> list[RunEvent]:
        async with self._lock:
            timeline = self._runs.get(run_id)
            if timeline is None:
                return []
            self._runs.move_to_end(run_id)
            return [event for event in timeline.events if event.event_seq > after_seq]

    async def get_status(self, run_id: str) -> RunStatus | None:
        async with self._lock:
            timeline = self._runs.get(run_id)
            if timeline is None:
                return None
            return timeline.status

    async def get_metadata(self, run_id: str) -> dict[str, Any] | None:
        async with self._lock:
            timeline = self._runs.get(run_id)
            if timeline is None:
                return None
            return {
                "run_id": run_id,
                "status": timeline.status.value,
                "request_id": timeline.request_id,
                "tenant_id": timeline.tenant_id,
                "session_id": timeline.session_id,
                "model": timeline.model,
                "created_at": timeline.created_at,
                "updated_at": timeline.updated_at,
                "event_count": len(timeline.events),
                **self._terminal_metadata(timeline.events),
            }

    @staticmethod
    def _terminal_metadata(events: list[RunEvent]) -> dict[str, Any]:
        terminal = next(
            (event for event in reversed(events) if event.event_type.startswith("run_")),
            None,
        )
        if terminal is None:
            return {"terminal_reason": None, "retryable": None}
        return {
            "terminal_reason": terminal.payload.get("terminal_reason"),
            "retryable": terminal.payload.get("retryable"),
        }

    async def clear(self) -> None:
        async with self._lock:
            self._runs.clear()

    def _append_locked(
        self,
        run_id: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> RunEvent:
        timeline = self._runs[run_id]
        event = RunEvent(
            run_id=run_id,
            event_seq=len(timeline.events) + 1,
            event_type=event_type,
            timestamp=time.time(),
            payload=payload,
        )
        timeline.events.append(event)
        timeline.updated_at = event.timestamp
        self._runs.move_to_end(run_id)
        return event

    def _evict_if_needed(self) -> None:
        while len(self._runs) > self._max_runs:
            self._runs.popitem(last=False)


class StoreBackedRunRecorder:
    """Run recorder adapter backed by a durable ``RunStore`` implementation."""

    def __init__(self, store: RunStore):
        self._store = store

    async def start_run(
        self,
        *,
        run_id: str,
        request_id: str,
        tenant_id: str,
        session_id: str,
        model: str,
        metadata: dict[str, Any] | None = None,
    ) -> RunEvent:
        return await self._store.start_run(
            run_id=run_id,
            request_id=request_id,
            tenant_id=tenant_id,
            session_id=session_id,
            model=model,
            metadata=metadata,
        )

    async def record_event(
        self,
        run_id: str,
        event_type: str,
        payload: dict[str, Any] | None = None,
    ) -> RunEvent:
        return await self._store.append_event(run_id, event_type, payload or {})

    async def finish_run(
        self,
        run_id: str,
        status: RunStatus,
        payload: dict[str, Any] | None = None,
    ) -> RunEvent:
        return await self._store.finish_run(run_id, status, payload or {})

    async def list_events(self, run_id: str, after_seq: int = 0) -> list[RunEvent]:
        return await self._store.list_events(run_id, after_seq)

    async def get_status(self, run_id: str) -> RunStatus | None:
        return await self._store.get_status(run_id)

    async def get_metadata(self, run_id: str) -> dict[str, Any] | None:
        return await self._store.get_metadata(run_id)

    async def clear(self) -> None:
        await self._store.clear()


class RunRecorder(Protocol):
    """Recorder interface consumed by API and graph execution code."""

    async def start_run(self, **kwargs: Any) -> RunEvent: ...

    async def record_event(
        self,
        run_id: str,
        event_type: str,
        payload: dict[str, Any] | None = None,
    ) -> RunEvent: ...

    async def finish_run(
        self,
        run_id: str,
        status: RunStatus,
        payload: dict[str, Any] | None = None,
    ) -> RunEvent: ...

    async def list_events(self, run_id: str, after_seq: int = 0) -> list[RunEvent]: ...

    async def get_status(self, run_id: str) -> RunStatus | None: ...

    async def get_metadata(self, run_id: str) -> dict[str, Any] | None: ...

    async def clear(self) -> None: ...


def build_run_recorder(*, backend: str, sqlite_path: str) -> RunRecorder:
    """Build a recorder from explicit settings so the choice is testable."""
    normalized_backend = backend.strip().lower()
    if normalized_backend == "memory":
        return InMemoryRunRecorder()
    if normalized_backend == "sqlite":
        from app.runtime.store import SQLiteRuntimeStore

        return StoreBackedRunRecorder(SQLiteRuntimeStore(sqlite_path))
    raise ValueError(f"unsupported runtime store backend: {backend!r}")


def _build_default_recorder() -> RunRecorder:
    from app.config import settings

    return build_run_recorder(
        backend=settings.runtime_store_backend,
        sqlite_path=settings.runtime_store_sqlite_path,
    )


_recorder = _build_default_recorder()


def get_run_recorder() -> RunRecorder:
    return _recorder


async def reset_run_recorder_for_tests() -> None:
    await _recorder.clear()
