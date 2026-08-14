from __future__ import annotations

from types import SimpleNamespace

from app.runtime.observers import ObserverDispatcher, TurnObserverContext
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