"""Idempotency store contract for runtime tool invocations.

This is intentionally in-memory for the first implementation slice. The API
models the behavior we need from a later durable store:

- same key + same args while processing -> in-progress
- same key + same args after success -> replay cached result
- same key + different args -> conflict
- failed records can be retried only when the caller asks for it
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol


class IdempotencyStatus(str, Enum):
    PROCESSING = "processing"
    SUCCESS = "success"
    FAILED = "failed"


class IdempotencyDecision(str, Enum):
    CLAIMED = "claimed"
    IN_PROGRESS = "in_progress"
    REPLAY_SUCCESS = "replay_success"
    RETRY_CLAIMED = "retry_claimed"
    REPLAY_FAILED = "replay_failed"
    CONFLICT = "conflict"


@dataclass(frozen=True)
class IdempotencyRecord:
    idempotency_key: str
    args_hash: str
    status: IdempotencyStatus
    result: Any | None = None
    error_class: str | None = None
    error_message: str | None = None
    created_at: float = 0.0
    updated_at: float = 0.0


@dataclass(frozen=True)
class IdempotencyClaim:
    decision: IdempotencyDecision
    record: IdempotencyRecord

    @property
    def should_execute(self) -> bool:
        return self.decision in {
            IdempotencyDecision.CLAIMED,
            IdempotencyDecision.RETRY_CLAIMED,
        }


class IdempotencyStore(Protocol):
    async def claim(
        self,
        *,
        idempotency_key: str,
        args_hash: str,
        retry_failed: bool = False,
        invocation: dict[str, Any] | None = None,
    ) -> IdempotencyClaim: ...

    async def complete(self, idempotency_key: str, result: Any) -> IdempotencyRecord: ...

    async def fail(
        self,
        idempotency_key: str,
        *,
        error_class: str,
        error_message: str,
    ) -> IdempotencyRecord: ...

    async def clear(self) -> None: ...

    async def recover_run(self, run_id: str) -> int: ...


class InMemoryIdempotencyStore:
    """Bounded process-local idempotency store.

    This is not durable across restarts. It is a small, testable stand-in for
    the future Postgres-backed table described in the roadmap.
    """

    def __init__(self, max_records: int = 1000):
        self._records: OrderedDict[str, IdempotencyRecord] = OrderedDict()
        self._max_records = max_records
        self._lock = asyncio.Lock()

    async def claim(
        self,
        *,
        idempotency_key: str,
        args_hash: str,
        retry_failed: bool = False,
        invocation: dict[str, Any] | None = None,
    ) -> IdempotencyClaim:
        async with self._lock:
            existing = self._records.get(idempotency_key)
            now = time.time()

            if existing is None:
                record = IdempotencyRecord(
                    idempotency_key=idempotency_key,
                    args_hash=args_hash,
                    status=IdempotencyStatus.PROCESSING,
                    created_at=now,
                    updated_at=now,
                )
                self._records[idempotency_key] = record
                self._evict_if_needed()
                return IdempotencyClaim(IdempotencyDecision.CLAIMED, record)

            self._records.move_to_end(idempotency_key)
            if existing.args_hash != args_hash:
                return IdempotencyClaim(IdempotencyDecision.CONFLICT, existing)

            if existing.status == IdempotencyStatus.PROCESSING:
                return IdempotencyClaim(IdempotencyDecision.IN_PROGRESS, existing)

            if existing.status == IdempotencyStatus.SUCCESS:
                return IdempotencyClaim(IdempotencyDecision.REPLAY_SUCCESS, existing)

            if retry_failed:
                record = IdempotencyRecord(
                    idempotency_key=idempotency_key,
                    args_hash=args_hash,
                    status=IdempotencyStatus.PROCESSING,
                    created_at=existing.created_at,
                    updated_at=now,
                )
                self._records[idempotency_key] = record
                return IdempotencyClaim(IdempotencyDecision.RETRY_CLAIMED, record)

            return IdempotencyClaim(IdempotencyDecision.REPLAY_FAILED, existing)

    async def complete(self, idempotency_key: str, result: Any) -> IdempotencyRecord:
        async with self._lock:
            existing = self._require_existing(idempotency_key)
            record = IdempotencyRecord(
                idempotency_key=existing.idempotency_key,
                args_hash=existing.args_hash,
                status=IdempotencyStatus.SUCCESS,
                result=result,
                created_at=existing.created_at,
                updated_at=time.time(),
            )
            self._records[idempotency_key] = record
            self._records.move_to_end(idempotency_key)
            return record

    async def fail(
        self,
        idempotency_key: str,
        *,
        error_class: str,
        error_message: str,
    ) -> IdempotencyRecord:
        async with self._lock:
            existing = self._require_existing(idempotency_key)
            record = IdempotencyRecord(
                idempotency_key=existing.idempotency_key,
                args_hash=existing.args_hash,
                status=IdempotencyStatus.FAILED,
                error_class=error_class,
                error_message=error_message,
                created_at=existing.created_at,
                updated_at=time.time(),
            )
            self._records[idempotency_key] = record
            self._records.move_to_end(idempotency_key)
            return record

    async def get(self, idempotency_key: str) -> IdempotencyRecord | None:
        async with self._lock:
            record = self._records.get(idempotency_key)
            if record:
                self._records.move_to_end(idempotency_key)
            return record

    async def clear(self) -> None:
        async with self._lock:
            self._records.clear()

    async def recover_run(self, run_id: str) -> int:
        return 0

    def _require_existing(self, idempotency_key: str) -> IdempotencyRecord:
        existing = self._records.get(idempotency_key)
        if existing is None:
            raise KeyError(f"idempotency key not claimed: {idempotency_key}")
        return existing

    def _evict_if_needed(self) -> None:
        while len(self._records) > self._max_records:
            self._records.popitem(last=False)


def _build_default_store() -> IdempotencyStore:
    from app.config import settings

    if settings.runtime_store_backend.strip().lower() == "sqlite":
        from app.runtime.store import SQLiteRuntimeStore

        return SQLiteRuntimeStore(settings.runtime_store_sqlite_path)
    if settings.runtime_store_backend.strip().lower() == "memory":
        return InMemoryIdempotencyStore()
    raise ValueError(
        f"unsupported runtime store backend: {settings.runtime_store_backend!r}"
    )


_store = _build_default_store()


def get_idempotency_store() -> IdempotencyStore:
    return _store


async def reset_idempotency_store_for_tests() -> None:
    await _store.clear()
