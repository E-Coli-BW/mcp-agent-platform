from __future__ import annotations

from types import SimpleNamespace

from app.context.request_context import set_request_context
from app.runtime.cancellation import RunCancellationRegistry
from app.runtime.observers import ObserverDispatcher
from app.runtime.recorder import InMemoryRunRecorder, RunStatus
from app.runtime.runner import RunCoordinator, RuntimeTurnRequest


class FakeConversationStore:
    def __init__(self) -> None:
        self.messages = []

    async def append(self, session_id, message):
        self.messages.append((session_id, message))


class FakeSessionLane:
    def __init__(self, *, held: bool = False, acquired: bool = True) -> None:
        self.held = held
        self.acquired = acquired
        self.released = []

    async def is_held(self, _session_id):
        return self.held

    async def acquire(self, _session_id, max_wait):
        return self.acquired

    async def release(self, session_id):
        self.released.append(session_id)


class FakeAgentEngine:
    def __init__(self, events=()) -> None:
        self.events = events

    async def stream_turn(self, _request):
        async def event_stream():
            for event in self.events:
                yield event

        return event_stream()


class FakeFanIn:
    def __init__(self, *, agent_events, root_session_id) -> None:
        self._events = agent_events

    async def start(self):
        return None

    async def next_event(self, timeout):
        try:
            return "agent", await anext(self._events)
        except StopAsyncIteration:
            raise StopAsyncIteration from None

    async def emit(self, source, event):
        return None

    async def close(self):
        return None


def prompt_resolution(**_kwargs):
    return SimpleNamespace(
        prompt_id="coding-agent.system",
        version="v2",
        content_hash="sha256:test",
    )


def runtime_request() -> RuntimeTurnRequest:
    return RuntimeTurnRequest(
        model="coding-agent",
        session_id="tenant:session",
        tenant_id="tenant",
        user_input="inspect the failure",
        temperature=0.0,
        prompt_version="v2",
    )


async def test_session_lane_timeout_finishes_run():
    recorder = InMemoryRunRecorder()
    set_request_context(
        "request-lane-timeout",
        "run-lane-timeout",
        "tenant:session",
    )
    coordinator = RunCoordinator(
        FakeAgentEngine(),
        prompt_resolver=prompt_resolution,
        conversation_store=FakeConversationStore(),
        recorder=recorder,
        cancellation=RunCancellationRegistry(),
        session_lane=FakeSessionLane(held=True, acquired=False),
    )

    events = [event async for event in coordinator.stream_turn(runtime_request())]

    metadata = await recorder.get_metadata("run-lane-timeout")
    assert metadata is not None
    assert metadata["status"] == RunStatus.FAILED.value
    assert metadata["terminal_reason"] == "session_lane_timeout"
    assert metadata["retryable"] is True
    assert [event.kind for event in events] == ["status", "status", "content", "done"]


async def test_observer_failure_does_not_change_successful_turn():
    class FailingObserver:
        async def observe(self, _observation):
            raise RuntimeError("telemetry offline")

    def failing_observers(context, _recorder):
        return ObserverDispatcher(context, [FailingObserver()])

    recorder = InMemoryRunRecorder()
    store = FakeConversationStore()
    lane = FakeSessionLane()
    set_request_context("request-1", "run-1", "tenant:session")
    events = (
        {
            "event": "on_chat_model_start",
            "metadata": {"langgraph_node": "call_llm"},
            "data": {"input": {"messages": []}},
        },
        {
            "event": "on_chat_model_stream",
            "metadata": {"langgraph_node": "call_llm"},
            "data": {"chunk": SimpleNamespace(content="completed")},
        },
        {
            "event": "on_chat_model_end",
            "metadata": {"langgraph_node": "call_llm"},
            "data": {},
        },
    )
    coordinator = RunCoordinator(
        FakeAgentEngine(events),
        observer_factory=failing_observers,
        prompt_resolver=prompt_resolution,
        conversation_store=store,
        recorder=recorder,
        cancellation=RunCancellationRegistry(),
        session_lane=lane,
        fan_in_factory=FakeFanIn,
    )

    visible = [event async for event in coordinator.stream_turn(runtime_request())]

    assert next(
        event.payload["content"]
        for event in visible
        if event.kind == "content" and event.payload["content"]
    ) == "completed"
    assert visible[-1].kind == "done"
    assert await recorder.get_status("run-1") == RunStatus.COMPLETED
    assert store.messages[-1][1].content == "completed"
    assert lane.released == ["tenant:session"]