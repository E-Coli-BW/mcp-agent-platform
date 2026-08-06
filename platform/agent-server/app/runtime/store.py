"""Durable runtime store contract and SQLite development implementation."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from app.runtime.recorder import RunEvent, RunStatus


class RuntimeStore(ABC):
    """Persistence contract for run metadata and ordered run events."""

    @abstractmethod
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
        """Create a run if needed and return the stable run_started event."""

    @abstractmethod
    async def append_event(
        self,
        run_id: str,
        event_type: str,
        payload: dict[str, Any] | None = None,
    ) -> RunEvent:
        """Append an event with the next sequence for ``run_id``."""

    @abstractmethod
    async def finish_run(
        self,
        run_id: str,
        status: RunStatus,
        payload: dict[str, Any] | None = None,
    ) -> RunEvent:
        """Mark a run terminal and append its terminal lifecycle event."""

    @abstractmethod
    async def list_events(self, run_id: str, after_seq: int = 0) -> list[RunEvent]:
        """Return ordered events after the supplied sequence cursor."""

    @abstractmethod
    async def get_status(self, run_id: str) -> RunStatus | None:
        """Return current run status if the run exists."""

    @abstractmethod
    async def get_metadata(self, run_id: str) -> dict[str, Any] | None:
        """Return run metadata and event count if the run exists."""

    @abstractmethod
    async def clear(self) -> None:
        """Delete all runtime rows. Intended for tests and local dev reset."""


class SQLiteRuntimeStore(RuntimeStore):
    """SQLite-backed runtime store.

    SQLite gives the project a real schema and restart-persistence tests without
    adding a service dependency. The table layout intentionally maps cleanly to
    a future Postgres implementation.
    """

    def __init__(self, database_path: str | Path):
        self._database_path = Path(database_path)
        if self._database_path.parent != Path("."):
            self._database_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        self._init_schema()

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
        async with self._lock:
            with self._connect() as conn:
                existing = self._get_event(conn, run_id, 1)
                if existing is not None:
                    return existing

                now = time.time()
                conn.execute(
                    """
                    INSERT OR IGNORE INTO agent_runs (
                        run_id, status, request_id, tenant_id, session_id,
                        model, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        RunStatus.RUNNING.value,
                        request_id,
                        tenant_id,
                        session_id,
                        model,
                        now,
                        now,
                    ),
                )
                return self._append_event_locked(
                    conn,
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

    async def append_event(
        self,
        run_id: str,
        event_type: str,
        payload: dict[str, Any] | None = None,
    ) -> RunEvent:
        async with self._lock:
            with self._connect() as conn:
                self._ensure_run_locked(conn, run_id, RunStatus.RUNNING)
                return self._append_event_locked(conn, run_id, event_type, payload or {})

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
            with self._connect() as conn:
                self._ensure_run_locked(conn, run_id, status)
                conn.execute(
                    "UPDATE agent_runs SET status = ?, updated_at = ? WHERE run_id = ?",
                    (status.value, time.time(), run_id),
                )
                return self._append_event_locked(conn, run_id, event_type, payload or {})

    async def list_events(self, run_id: str, after_seq: int = 0) -> list[RunEvent]:
        async with self._lock:
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT run_id, event_seq, event_type, timestamp, payload_json
                    FROM agent_run_events
                    WHERE run_id = ? AND event_seq > ?
                    ORDER BY event_seq ASC
                    """,
                    (run_id, after_seq),
                ).fetchall()
                return [self._event_from_row(row) for row in rows]

    async def get_status(self, run_id: str) -> RunStatus | None:
        async with self._lock:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT status FROM agent_runs WHERE run_id = ?",
                    (run_id,),
                ).fetchone()
                if row is None:
                    return None
                return RunStatus(row["status"])

    async def get_metadata(self, run_id: str) -> dict[str, Any] | None:
        async with self._lock:
            with self._connect() as conn:
                row = conn.execute(
                    """
                    SELECT
                        r.run_id,
                        r.status,
                        r.request_id,
                        r.tenant_id,
                        r.session_id,
                        r.model,
                        r.created_at,
                        r.updated_at,
                        COUNT(e.event_seq) AS event_count
                    FROM agent_runs r
                    LEFT JOIN agent_run_events e ON e.run_id = r.run_id
                    WHERE r.run_id = ?
                    GROUP BY r.run_id
                    """,
                    (run_id,),
                ).fetchone()
                if row is None:
                    return None
                metadata = dict(row)
                terminal = conn.execute(
                    """SELECT event_type, payload_json FROM agent_run_events
                       WHERE run_id = ? AND event_type LIKE 'run_%'
                       ORDER BY event_seq DESC LIMIT 1""",
                    (run_id,),
                ).fetchone()
                payload = json.loads(terminal["payload_json"]) if terminal else {}
                metadata["terminal_reason"] = payload.get("terminal_reason")
                metadata["retryable"] = payload.get("retryable")
                return metadata

    async def clear(self) -> None:
        async with self._lock:
            with self._connect() as conn:
                conn.execute("DELETE FROM agent_run_events")
                conn.execute("DELETE FROM agent_tool_invocations")
                conn.execute("DELETE FROM agent_runs")

    async def recover_run(self, run_id: str) -> int:
        """Release processing invocations left by a crashed worker."""
        async with self._lock:
            with self._connect() as conn:
                result = conn.execute(
                    "DELETE FROM agent_tool_invocations WHERE run_id = ? AND status = 'processing'",
                    (run_id,),
                )
                return result.rowcount

    async def claim(
        self,
        *,
        idempotency_key: str,
        args_hash: str,
        retry_failed: bool = False,
        invocation: dict[str, Any] | None = None,
    ) -> IdempotencyClaim:
        from app.runtime.idempotency import (
            IdempotencyClaim,
            IdempotencyDecision,
            IdempotencyRecord,
            IdempotencyStatus,
        )

        async with self._lock:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT * FROM agent_tool_invocations WHERE idempotency_key = ?",
                    (idempotency_key,),
                ).fetchone()
                now = time.time()
                if row is None:
                    data = invocation or {}
                    conn.execute(
                        """INSERT INTO agent_tool_invocations
                        (tool_invocation_id, run_id, step_id, tool_name, tenant_id,
                         args_hash, idempotency_key, status, risk_level, created_at,
                         updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            data.get("tool_invocation_id", idempotency_key),
                            data.get("run_id", ""),
                            data.get("step_id", ""),
                            data.get("tool_name", "file_write"),
                            data.get("tenant_id", ""),
                            args_hash,
                            idempotency_key,
                            IdempotencyStatus.PROCESSING.value,
                            data.get("risk", "write"),
                            now,
                            now,
                        ),
                    )
                    return IdempotencyClaim(
                        IdempotencyDecision.CLAIMED,
                        IdempotencyRecord(idempotency_key, args_hash, IdempotencyStatus.PROCESSING, created_at=now, updated_at=now),
                    )
                record = self._idempotency_record(row)
                if record.args_hash != args_hash:
                    return IdempotencyClaim(IdempotencyDecision.CONFLICT, record)
                if record.status == IdempotencyStatus.PROCESSING:
                    return IdempotencyClaim(IdempotencyDecision.IN_PROGRESS, record)
                if record.status == IdempotencyStatus.SUCCESS:
                    return IdempotencyClaim(IdempotencyDecision.REPLAY_SUCCESS, record)
                if retry_failed:
                    conn.execute(
                        "UPDATE agent_tool_invocations SET status = ?, updated_at = ? WHERE idempotency_key = ?",
                        (IdempotencyStatus.PROCESSING.value, now, idempotency_key),
                    )
                    return IdempotencyClaim(IdempotencyDecision.RETRY_CLAIMED, record)
                return IdempotencyClaim(IdempotencyDecision.REPLAY_FAILED, record)

    async def complete(self, idempotency_key: str, result: Any) -> IdempotencyRecord:
        from app.runtime.idempotency import IdempotencyStatus

        return await self._update_idempotency(idempotency_key, IdempotencyStatus.SUCCESS, result=result)

    async def fail(self, idempotency_key: str, *, error_class: str, error_message: str) -> IdempotencyRecord:
        from app.runtime.idempotency import IdempotencyStatus

        return await self._update_idempotency(
            idempotency_key,
            IdempotencyStatus.FAILED,
            error_class=error_class,
            error_message=error_message,
        )

    async def _update_idempotency(self, idempotency_key: str, status: IdempotencyStatus, *, result: Any = None, error_class: str | None = None, error_message: str | None = None) -> IdempotencyRecord:
        from app.runtime.idempotency import IdempotencyStatus

        async with self._lock:
            with self._connect() as conn:
                row = conn.execute("SELECT * FROM agent_tool_invocations WHERE idempotency_key = ?", (idempotency_key,)).fetchone()
                if row is None:
                    raise KeyError(f"idempotency key not claimed: {idempotency_key}")
                now = time.time()
                conn.execute(
                    "UPDATE agent_tool_invocations SET status = ?, updated_at = ?, result_json = ?, error_class = ?, error_message = ? WHERE idempotency_key = ?",
                    (status.value, now, json.dumps(result) if result is not None else None, error_class, error_message, idempotency_key),
                )
                updated = dict(row)
                updated.update(status=status.value, updated_at=now, result_json=json.dumps(result) if result is not None else None, error_class=error_class, error_message=error_message)
                return self._idempotency_record(updated)

    @staticmethod
    def _idempotency_record(row: sqlite3.Row | dict[str, Any]) -> IdempotencyRecord:
        from app.runtime.idempotency import IdempotencyRecord, IdempotencyStatus

        result_json = row["result_json"]
        return IdempotencyRecord(
            idempotency_key=row["idempotency_key"], args_hash=row["args_hash"],
            status=IdempotencyStatus(row["status"]),
            result=json.loads(result_json) if result_json else None,
            error_class=row["error_class"], error_message=row["error_message"],
            created_at=row["created_at"], updated_at=row["updated_at"],
        )

    def _init_schema(self) -> None:
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(
                """
                PRAGMA foreign_keys = ON;

                CREATE TABLE IF NOT EXISTS agent_runs (
                    run_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    request_id TEXT NOT NULL DEFAULT '',
                    tenant_id TEXT NOT NULL DEFAULT '',
                    session_id TEXT NOT NULL DEFAULT '',
                    model TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS agent_run_events (
                    run_id TEXT NOT NULL,
                    event_seq INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    timestamp REAL NOT NULL,
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    PRIMARY KEY (run_id, event_seq),
                    FOREIGN KEY (run_id) REFERENCES agent_runs(run_id)
                        ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_agent_run_events_run_seq
                    ON agent_run_events(run_id, event_seq);

                CREATE TABLE IF NOT EXISTS agent_tool_invocations (
                    tool_invocation_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    step_id TEXT NOT NULL DEFAULT '',
                    tool_name TEXT NOT NULL,
                    tenant_id TEXT NOT NULL DEFAULT '',
                    args_hash TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    status TEXT NOT NULL,
                    risk_level TEXT NOT NULL DEFAULT 'low',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    result_json TEXT,
                    error_class TEXT,
                    error_message TEXT,
                    FOREIGN KEY (run_id) REFERENCES agent_runs(run_id)
                        ON DELETE CASCADE
                );

                CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_tool_invocations_idem
                    ON agent_tool_invocations(idempotency_key);
                """
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._database_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _ensure_run_locked(
        self,
        conn: sqlite3.Connection,
        run_id: str,
        status: RunStatus,
    ) -> None:
        row = conn.execute(
            "SELECT run_id FROM agent_runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if row is not None:
            return

        now = time.time()
        conn.execute(
            """
            INSERT INTO agent_runs (
                run_id, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?)
            """,
            (run_id, status.value, now, now),
        )

    def _append_event_locked(
        self,
        conn: sqlite3.Connection,
        run_id: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> RunEvent:
        row = conn.execute(
            "SELECT COALESCE(MAX(event_seq), 0) + 1 AS next_seq "
            "FROM agent_run_events WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        event_seq = int(row["next_seq"])
        timestamp = time.time()
        conn.execute(
            """
            INSERT INTO agent_run_events (
                run_id, event_seq, event_type, timestamp, payload_json
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (run_id, event_seq, event_type, timestamp, json.dumps(payload)),
        )
        conn.execute(
            "UPDATE agent_runs SET updated_at = ? WHERE run_id = ?",
            (timestamp, run_id),
        )
        return RunEvent(
            run_id=run_id,
            event_seq=event_seq,
            event_type=event_type,
            timestamp=timestamp,
            payload=payload,
        )

    def _get_event(
        self,
        conn: sqlite3.Connection,
        run_id: str,
        event_seq: int,
    ) -> RunEvent | None:
        row = conn.execute(
            """
            SELECT run_id, event_seq, event_type, timestamp, payload_json
            FROM agent_run_events
            WHERE run_id = ? AND event_seq = ?
            """,
            (run_id, event_seq),
        ).fetchone()
        if row is None:
            return None
        return self._event_from_row(row)

    def _event_from_row(self, row: sqlite3.Row) -> RunEvent:
        return RunEvent(
            run_id=row["run_id"],
            event_seq=row["event_seq"],
            event_type=row["event_type"],
            timestamp=row["timestamp"],
            payload=json.loads(row["payload_json"]),
        )
