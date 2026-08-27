from __future__ import annotations

from types import SimpleNamespace

from app.runtime.observers import ObserverDispatcher, TurnObserverContext
from app.runtime.observers.provenance import (
    ModelProvenanceObserver,
    model_performance_from_event,
    prompt_prefix_fingerprint,
)
from app.runtime.observers.base import RuntimeObservation, TurnObservationReport
from app.runtime.observers.usage import UsageObserver


def observer_context() -> TurnObserverContext:
    return TurnObserverContext(
        run_id="run-1",
        request_id="request-1",
        tenant_id="tenant-a",
        session_id="tenant-a:session-1",
        model="coding-agent",
        runtime="python-v2",
        temperature=0.0,
        max_tokens=1024,
        prompt_id="coding-agent-system",
        prompt_version="v2",
        prompt_hash="sha256:test",
        feature_flags={},
    )


async def test_observer_failure_does_not_block_other_observers():
    received = []

    class FailingObserver:
        async def observe(self, observation):
            raise RuntimeError("telemetry backend unavailable")

    class RecordingObserver:
        async def observe(self, observation):
            received.append(observation)

    dispatcher = ObserverDispatcher(
        observer_context(),
        [FailingObserver(), RecordingObserver()],
    )

    await dispatcher.observe("tool_started", {"tool": "file_read"})

    assert len(received) == 1
    assert received[0].kind == "tool_started"
    assert received[0].payload == {"tool": "file_read"}


async def test_usage_observer_accumulates_turn_report():
    recorded = []

    class FakeTracker:
        def record(self, usage):
            recorded.append(usage)

    dispatcher = ObserverDispatcher(observer_context())
    dispatcher.add(
        UsageObserver(
            dispatcher.context,
            dispatcher.report,
            tracker=FakeTracker(),
        )
    )
    message = SimpleNamespace(content="abcdefgh")
    chunk = SimpleNamespace(content="response")

    await dispatcher.observe(
        "model_started",
        raw_event={"data": {"input": {"messages": [[message]]}}},
    )
    await dispatcher.observe(
        "model_streamed",
        raw_event={"data": {"chunk": chunk}},
    )
    await dispatcher.observe("tool_started", {"seq": 1})
    await dispatcher.observe("turn_finished", {"duration_ms": 25})

    assert dispatcher.report.prompt_tokens == 2
    assert dispatcher.report.completion_tokens == 2
    assert dispatcher.report.tool_count == 1
    assert dispatcher.report.duration_ms == 25
    assert recorded[0].total_tokens == 4


def test_model_performance_extracts_ollama_timings():
    output = SimpleNamespace(
        response_metadata={
            "total_duration": 3_000_000_000,
            "load_duration": 100_000_000,
            "prompt_eval_count": 1_000,
            "prompt_eval_duration": 2_000_000_000,
            "eval_count": 50,
            "eval_duration": 500_000_000,
        }
    )

    performance = model_performance_from_event(
        {"data": {"output": output}}
    )

    assert performance == {
        "total_duration_ms": 3_000,
        "load_duration_ms": 100,
        "prefill_duration_ms": 2_000,
        "decode_duration_ms": 500,
        "prefill_tokens": 1_000,
        "decode_tokens": 50,
        "prefill_tokens_per_second": 500.0,
        "decode_tokens_per_second": 100.0,
        "ttft_ms": 2_100,
        "ttft_estimated": True,
    }


def test_prompt_prefix_fingerprint_tracks_leading_system_message():
    system = SimpleNamespace(content="stable system prompt")
    user = SimpleNamespace(content="changing request")

    prefix_hash, prefix_chars = prompt_prefix_fingerprint(
        {"data": {"input": {"messages": [[system, user]]}}}
    )

    assert prefix_hash is not None
    assert prefix_hash.startswith("sha256:")
    assert prefix_chars == len(system.content)


async def test_provenance_uses_internal_first_chunk_for_measured_ttft(monkeypatch):
    recorded_events = []
    emitted_calls = []

    class FakeRecorder:
        async def record_event(self, _run_id, event_type, payload):
            recorded_events.append((event_type, payload))

    class FakeProducer:
        async def emit_model_call(self, event):
            emitted_calls.append(event)

    clock = iter([10.0, 10.25, 11.0])
    monkeypatch.setattr(
        "app.runtime.observers.provenance.time",
        SimpleNamespace(monotonic=lambda: next(clock)),
    )
    observer = ModelProvenanceObserver(
        observer_context(),
        TurnObservationReport(),
        FakeRecorder(),
        producer=FakeProducer(),
    )
    message = SimpleNamespace(content="system prompt")
    await observer.observe(
        RuntimeObservation(
            "model_started",
            raw_event={"data": {"input": {"messages": [[message]]}}},
        )
    )
    await observer.observe(
        RuntimeObservation(
            "model_streamed",
            raw_event={"data": {"chunk": SimpleNamespace(content="first")}},
        )
    )
    output = SimpleNamespace(
        response_metadata={
            "load_duration": 100_000_000,
            "prompt_eval_count": 10,
            "prompt_eval_duration": 400_000_000,
            "eval_count": 2,
            "eval_duration": 100_000_000,
        }
    )
    await observer.observe(
        RuntimeObservation(
            "model_completed",
            raw_event={"data": {"output": output}},
        )
    )

    assert emitted_calls[0]["performance"]["ttft_ms"] == 250
    assert emitted_calls[0]["performance"]["ttft_estimated"] is False
    completed = next(payload for kind, payload in recorded_events if kind == "model_call_completed")
    assert completed["performance"]["ttft_ms"] == 250