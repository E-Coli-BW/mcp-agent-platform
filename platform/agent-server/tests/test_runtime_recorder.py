from __future__ import annotations

import pytest

from app.runtime.recorder import (
    InMemoryRunRecorder,
    RunStatus,
    StoreBackedRunRecorder,
    build_run_recorder,
)
from app.runtime.store import SQLiteRuntimeStore


@pytest.mark.asyncio
async def test_recorder_builds_ordered_run_timeline():
    recorder = InMemoryRunRecorder()

    await recorder.start_run(
        run_id="run-1",
        request_id="req-1",
        tenant_id="tenant-a",
        session_id="tenant-a:s1",
        model="coding-agent",
    )
    await recorder.record_event("run-1", "model_call_started", {"seq": 1})
    await recorder.record_event("run-1", "tool_started", {"tool": "file_read"})
    await recorder.record_event("run-1", "tool_completed", {"tool": "file_read"})
    await recorder.finish_run("run-1", RunStatus.COMPLETED, {"duration_ms": 42})

    events = await recorder.list_events("run-1")

    assert [event.event_seq for event in events] == [1, 2, 3, 4, 5]
    assert [event.event_type for event in events] == [
        "run_started",
        "model_call_started",
        "tool_started",
        "tool_completed",
        "run_completed",
    ]
    assert events[0].event_id == "run-1:1"
    assert await recorder.get_status("run-1") == RunStatus.COMPLETED


@pytest.mark.asyncio
async def test_start_run_is_idempotent_for_existing_run():
    recorder = InMemoryRunRecorder()

    first = await recorder.start_run(
        run_id="run-1",
        request_id="req-1",
        tenant_id="tenant-a",
        session_id="tenant-a:s1",
        model="coding-agent",
    )
    second = await recorder.start_run(
        run_id="run-1",
        request_id="req-duplicate",
        tenant_id="tenant-a",
        session_id="tenant-a:s1",
        model="coding-agent",
    )

    events = await recorder.list_events("run-1")

    assert first == second
    assert len(events) == 1
    assert events[0].payload["request_id"] == "req-1"


@pytest.mark.asyncio
async def test_list_events_supports_after_seq_cursor():
    recorder = InMemoryRunRecorder()

    await recorder.start_run(
        run_id="run-1",
        request_id="req-1",
        tenant_id="tenant-a",
        session_id="tenant-a:s1",
        model="coding-agent",
    )
    await recorder.record_event("run-1", "status_thinking")
    await recorder.finish_run("run-1", RunStatus.FAILED, {"error_class": "Boom"})

    events = await recorder.list_events("run-1", after_seq=1)

    assert [event.event_type for event in events] == ["status_thinking", "run_failed"]
    assert events[-1].payload["error_class"] == "Boom"


@pytest.mark.asyncio
async def test_sqlite_store_backed_recorder_persists_run_timeline(tmp_path):
    database_path = tmp_path / "runtime.sqlite3"
    recorder = StoreBackedRunRecorder(SQLiteRuntimeStore(database_path))

    first_start = await recorder.start_run(
        run_id="run-1",
        request_id="req-1",
        tenant_id="tenant-a",
        session_id="tenant-a:s1",
        model="coding-agent",
    )
    second_start = await recorder.start_run(
        run_id="run-1",
        request_id="req-duplicate",
        tenant_id="tenant-a",
        session_id="tenant-a:s1",
        model="coding-agent",
    )
    await recorder.record_event("run-1", "tool_started", {"tool": "file_read"})
    await recorder.finish_run("run-1", RunStatus.COMPLETED, {"duration_ms": 42})

    restarted_recorder = StoreBackedRunRecorder(SQLiteRuntimeStore(database_path))
    events = await restarted_recorder.list_events("run-1")
    metadata = await restarted_recorder.get_metadata("run-1")

    assert first_start == second_start
    assert [event.event_seq for event in events] == [1, 2, 3]
    assert [event.event_type for event in events] == [
        "run_started",
        "tool_started",
        "run_completed",
    ]
    assert events[0].payload["request_id"] == "req-1"
    assert await restarted_recorder.get_status("run-1") == RunStatus.COMPLETED
    assert metadata is not None
    assert metadata["tenant_id"] == "tenant-a"
    assert metadata["event_count"] == 3
    assert metadata["terminal_reason"] is None
    assert metadata["retryable"] is None


@pytest.mark.asyncio
async def test_terminal_metadata_is_exposed_for_failed_run():
    recorder = InMemoryRunRecorder()

    await recorder.start_run(
        run_id="run-failed",
        request_id="req-1",
        tenant_id="tenant-a",
        session_id="tenant-a:s1",
        model="coding-agent",
    )
    await recorder.finish_run(
        "run-failed",
        RunStatus.FAILED,
        {"terminal_reason": "agent_error", "retryable": True},
    )

    metadata = await recorder.get_metadata("run-failed")

    assert metadata is not None
    assert metadata["terminal_reason"] == "agent_error"
    assert metadata["retryable"] is True


def test_build_run_recorder_defaults_to_memory():
    recorder = build_run_recorder(backend="memory", sqlite_path="unused.sqlite3")

    assert isinstance(recorder, InMemoryRunRecorder)


def test_build_run_recorder_supports_sqlite(tmp_path):
    database_path = tmp_path / "nested" / "runtime.sqlite3"
    recorder = build_run_recorder(
        backend="sqlite",
        sqlite_path=str(database_path),
    )

    assert isinstance(recorder, StoreBackedRunRecorder)
    assert database_path.exists()


def test_build_run_recorder_rejects_unknown_backend():
    with pytest.raises(ValueError, match="unsupported runtime store backend"):
        build_run_recorder(backend="redis", sqlite_path="unused.sqlite3")
