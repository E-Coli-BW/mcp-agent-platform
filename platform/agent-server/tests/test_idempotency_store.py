from __future__ import annotations

import pytest

from app.runtime.idempotency import (
    IdempotencyDecision,
    IdempotencyStatus,
    InMemoryIdempotencyStore,
)
from app.runtime.store import SQLiteRuntimeStore


@pytest.mark.asyncio
async def test_claim_complete_then_replay_success():
    store = InMemoryIdempotencyStore()

    first = await store.claim(idempotency_key="key-1", args_hash="sha256:a")
    assert first.decision == IdempotencyDecision.CLAIMED
    assert first.should_execute is True

    await store.complete("key-1", {"ok": True})
    replay = await store.claim(idempotency_key="key-1", args_hash="sha256:a")

    assert replay.decision == IdempotencyDecision.REPLAY_SUCCESS
    assert replay.should_execute is False
    assert replay.record.status == IdempotencyStatus.SUCCESS
    assert replay.record.result == {"ok": True}


@pytest.mark.asyncio
async def test_duplicate_processing_returns_in_progress():
    store = InMemoryIdempotencyStore()

    await store.claim(idempotency_key="key-1", args_hash="sha256:a")
    duplicate = await store.claim(idempotency_key="key-1", args_hash="sha256:a")

    assert duplicate.decision == IdempotencyDecision.IN_PROGRESS
    assert duplicate.should_execute is False


@pytest.mark.asyncio
async def test_same_key_different_args_conflicts():
    store = InMemoryIdempotencyStore()

    await store.claim(idempotency_key="key-1", args_hash="sha256:a")
    conflict = await store.claim(idempotency_key="key-1", args_hash="sha256:b")

    assert conflict.decision == IdempotencyDecision.CONFLICT
    assert conflict.should_execute is False
    assert conflict.record.args_hash == "sha256:a"


@pytest.mark.asyncio
async def test_failed_record_retries_only_when_requested():
    store = InMemoryIdempotencyStore()

    await store.claim(idempotency_key="key-1", args_hash="sha256:a")
    await store.fail("key-1", error_class="TimeoutError", error_message="slow")

    replay_failed = await store.claim(idempotency_key="key-1", args_hash="sha256:a")
    assert replay_failed.decision == IdempotencyDecision.REPLAY_FAILED
    assert replay_failed.should_execute is False
    assert replay_failed.record.error_class == "TimeoutError"

    retry = await store.claim(
        idempotency_key="key-1",
        args_hash="sha256:a",
        retry_failed=True,
    )
    assert retry.decision == IdempotencyDecision.RETRY_CLAIMED
    assert retry.should_execute is True
    assert retry.record.status == IdempotencyStatus.PROCESSING


@pytest.mark.asyncio
async def test_sqlite_idempotency_survives_fresh_store(tmp_path):
    database_path = tmp_path / "nested" / "runtime.sqlite3"
    first_store = SQLiteRuntimeStore(database_path)
    await first_store.start_run(
        run_id="run-1",
        request_id="req-1",
        tenant_id="tenant-a",
        session_id="tenant-a:s1",
        model="coding-agent",
    )
    first = await first_store.claim(
        idempotency_key="tool-key-1",
        args_hash="sha256:a",
        invocation={
            "tool_invocation_id": "tinv-1",
            "run_id": "run-1",
            "tool_name": "file_write",
            "tenant_id": "tenant-a",
            "risk": "write",
        },
    )
    assert first.decision == IdempotencyDecision.CLAIMED
    await first_store.complete("tool-key-1", "written")

    restarted_store = SQLiteRuntimeStore(database_path)
    replay = await restarted_store.claim(
        idempotency_key="tool-key-1",
        args_hash="sha256:a",
    )

    assert replay.decision == IdempotencyDecision.REPLAY_SUCCESS
    assert replay.record.result == "written"
