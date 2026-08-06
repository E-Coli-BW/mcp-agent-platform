"""Durable, serializable runtime checkpoints for resumable runs."""
from __future__ import annotations
import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path


class InvalidCheckpointTransition(ValueError):
    """Raised when a checkpoint attempts to regress or leave a terminal state."""


_ALLOWED_TRANSITIONS = {
    "waiting_approval": {"approved", "rejected", "failed"},
    "approved": {"executing", "completed", "failed"},
    "executing": {"completed", "failed"},
    "completed": set(),
    "failed": set(),
    "rejected": set(),
}


@dataclass(frozen=True)
class RunCheckpoint:
    run_id: str
    step_seq: int
    state: str
    tool_name: str
    approval_id: str | None
    payload: dict
    lease_owner: str | None
    lease_expires_at: float | None
    updated_at: float

    def to_dict(self) -> dict:
        return self.__dict__.copy()

class SQLiteCheckpointStore:
    def __init__(self, database_path: str | Path):
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.database_path) as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS agent_run_checkpoints (
                run_id TEXT PRIMARY KEY, step_seq INTEGER NOT NULL,
                state TEXT NOT NULL, tool_name TEXT NOT NULL DEFAULT '',
                approval_id TEXT, payload_json TEXT NOT NULL DEFAULT '{}',
                lease_owner TEXT, lease_expires_at REAL, updated_at REAL NOT NULL
            )""")
    def save(self, *, run_id: str, step_seq: int, state: str, tool_name: str = "", approval_id: str | None = None, payload: dict | None = None):
        now = time.time()
        with sqlite3.connect(self.database_path) as conn:
            conn.execute("""INSERT OR REPLACE INTO agent_run_checkpoints
                (run_id, step_seq, state, tool_name, approval_id, payload_json, lease_owner, lease_expires_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, ?)""", (run_id, step_seq, state, tool_name, approval_id, json.dumps(payload or {}), now))
        return self.get(run_id)

    def save_next(self, *, run_id: str, state: str, tool_name: str = "", approval_id: str | None = None, payload: dict | None = None):
        """Atomically replace a terminal step with the next run-local step."""
        now = time.time()
        with sqlite3.connect(self.database_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT step_seq, state FROM agent_run_checkpoints WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                step_seq = 1
            else:
                previous_step, previous_state = row
                if previous_state not in {"completed", "failed", "rejected"}:
                    raise InvalidCheckpointTransition(
                        f"cannot start next checkpoint while step is {previous_state}"
                    )
                step_seq = previous_step + 1
            conn.execute("""INSERT OR REPLACE INTO agent_run_checkpoints
                (run_id, step_seq, state, tool_name, approval_id, payload_json, lease_owner, lease_expires_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, ?)""",
                (run_id, step_seq, state, tool_name, approval_id, json.dumps(payload or {}), now))
        return self.get(run_id)
    def get(self, run_id: str):
        with sqlite3.connect(self.database_path) as conn:
            row = conn.execute("SELECT * FROM agent_run_checkpoints WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            return None
        return RunCheckpoint(row[0], row[1], row[2], row[3], row[4], json.loads(row[5]), row[6], row[7], row[8])
    def claim(self, run_id: str, *, owner: str, lease_seconds: int = 60, expected_state: str | None = None):
        now = time.time()
        state_clause = " AND state = ?" if expected_state is not None else ""
        params = [owner, now + lease_seconds, now, run_id]
        if expected_state is not None:
            params.append(expected_state)
        params.append(now)
        with sqlite3.connect(self.database_path) as conn:
            result = conn.execute(f"""UPDATE agent_run_checkpoints SET lease_owner = ?, lease_expires_at = ?, updated_at = ?
                WHERE run_id = ?{state_clause}
                AND (lease_expires_at IS NULL OR lease_expires_at < ?)""", params)
            if result.rowcount != 1:
                return None
        return self.get(run_id)

    def list_recoverable(self) -> list[RunCheckpoint]:
        now = time.time()
        with sqlite3.connect(self.database_path) as conn:
            rows = conn.execute("""SELECT * FROM agent_run_checkpoints
                WHERE state IN ('waiting_approval', 'approved', 'executing')
                AND (lease_expires_at IS NULL OR lease_expires_at < ?)
                ORDER BY updated_at ASC""", (now,)).fetchall()
        return [RunCheckpoint(row[0], row[1], row[2], row[3], row[4], json.loads(row[5]), row[6], row[7], row[8]) for row in rows]

    def transition(self, run_id: str, *, state: str, payload: dict | None = None) -> RunCheckpoint | None:
        with sqlite3.connect(self.database_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT state, payload_json FROM agent_run_checkpoints WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                return None
            current_state, payload_json = row
            if state != current_state and state not in _ALLOWED_TRANSITIONS.get(
                current_state, set()
            ):
                raise InvalidCheckpointTransition(
                    f"invalid checkpoint transition: {current_state} -> {state}"
                )
            merged = json.loads(payload_json)
            merged.update(payload or {})
            now = time.time()
            result = conn.execute(
                """UPDATE agent_run_checkpoints
                SET state = ?, payload_json = ?, updated_at = ?
                WHERE run_id = ? AND state = ?""",
                (state, json.dumps(merged), now, run_id, current_state),
            )
            if result.rowcount != 1:
                raise InvalidCheckpointTransition(
                    f"checkpoint changed concurrently while transitioning {run_id}"
                )
        return self.get(run_id)

def get_checkpoint_store():
    from app.config import settings
    return SQLiteCheckpointStore(settings.runtime_store_sqlite_path)
