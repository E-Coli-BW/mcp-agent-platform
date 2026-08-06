"""Lease-protected continuation worker for durable runtime checkpoints."""
from __future__ import annotations

from typing import Callable, Any, Awaitable

from app.runtime.checkpoint import SQLiteCheckpointStore, RunCheckpoint


class ContinuationWorker:
    def __init__(self, store: SQLiteCheckpointStore, owner: str):
        self.store = store
        self.owner = owner

    def run_once(self, run_id: str, handler: Callable[[RunCheckpoint], Any]) -> RunCheckpoint | None:
        checkpoint = self.store.get(run_id)
        if checkpoint is None or checkpoint.state != "approved":
            return checkpoint
        claimed = self.store.claim(run_id, owner=self.owner, expected_state="approved")
        if claimed is None:
            return None
        executing = self.store.transition(run_id, state="executing")
        try:
            result = handler(executing)
            return self.store.transition(run_id, state="completed", payload={"result": result})
        except Exception as exc:
            return self.store.transition(
                run_id,
                state="failed",
                payload={"error_class": exc.__class__.__name__, "error_message": str(exc)},
            )

    async def run_once_async(self, run_id: str, handler: Callable[[RunCheckpoint], Awaitable[Any]]) -> RunCheckpoint | None:
        checkpoint = self.store.get(run_id)
        if checkpoint is None or checkpoint.state != "approved":
            return checkpoint
        claimed = self.store.claim(run_id, owner=self.owner, expected_state="approved")
        if claimed is None:
            return None
        executing = self.store.transition(run_id, state="executing")
        try:
            result = await handler(executing)
            return self.store.transition(run_id, state="completed", payload={"result": result})
        except Exception as exc:
            return self.store.transition(run_id, state="failed", payload={"error_class": exc.__class__.__name__, "error_message": str(exc)})
