"""Tests for fleet_bus + streaming/cancel behavior in subagent.py.

These tests exercise P1 #5: the per-root-session pub/sub bus that
surfaces child agent events to subscribers (e.g. chat.py SSE stream)
and the cooperative cancellation hook.

The tests deliberately stub out the actual LLM (via _build_child_agent
monkeypatching, same pattern as test_subagent.py) so we can assert on
event shape and cancel semantics without flakiness from a real model.

Run:
    cd platform/agent-server
    .venv/bin/python -m pytest tests/test_fleet_bus.py -q
"""
from __future__ import annotations

import asyncio
import time
from unittest.mock import MagicMock

import pytest

from app.agent import fleet_bus
from app.agent.subagent_context import (
    SubagentContext,
    init_root_context,
    subagent_context,
)


# ── Fixtures ───────────────────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def _reset_state():
    """Reset both the subagent ContextVar and the fleet_bus between tests.

    Without these, a test that registers session "test-root" leaks state
    into the next test which then sees pre-existing subscribers / cancel
    events. Both layers must be cleared.
    """
    token = subagent_context.set(None)  # type: ignore[arg-type]
    fleet_bus._reset_for_tests()
    yield
    fleet_bus._reset_for_tests()
    subagent_context.reset(token)


@pytest.fixture
def root_ctx():
    """A root context with reasonable budget for streaming tests."""
    return init_root_context(
        root_session_id="test-root",
        allowed_tools=["file_read"],
        token_budget=10_000,
        deadline_ms=30_000,
    )


# ── Minimal LangChain message stand-ins ────────────────────────────────────
class _FakeMsg:
    def __init__(self, content: str):
        self.content = content


class _FakeChunk:
    def __init__(self, content: str):
        self.content = content


# ── Direct fleet_bus tests ─────────────────────────────────────────────────
class TestFleetBusBasics:
    """Verify the bus primitives in isolation, no subagent involved."""

    @pytest.mark.asyncio
    async def test_should_dropEvent_when_sessionNotRegistered(self):
        """Publishing to an unknown session must be a silent no-op so that
        callers without a registered bus (unit tests, eval harness) work
        without setup. The alternative — raising — would force every
        test to register a bus or stub the publisher."""
        # Should not raise.
        fleet_bus.publish_event(
            root_session_id="ghost-session",
            child_session_id="c1",
            role="r",
            event_type="child_start",
            depth=1,
            brief_preview="hi",
        )
        assert fleet_bus._session_count() == 0

    @pytest.mark.asyncio
    async def test_should_deliverEvent_when_subscriberPresent(self):
        """Single subscriber receives a single published event."""
        await fleet_bus.register_session("s1")

        received: list[dict] = []

        async def reader():
            async for ev in fleet_bus.subscribe("s1"):
                received.append(ev)
                if ev["type"] == "child_end":
                    return

        task = asyncio.create_task(reader())
        # Give the subscriber a tick to install its queue.
        await asyncio.sleep(0)

        fleet_bus.publish_event(
            root_session_id="s1",
            child_session_id="c1",
            role="reader",
            event_type="child_token",
            token="hello",
        )
        fleet_bus.publish_event(
            root_session_id="s1",
            child_session_id="c1",
            role="reader",
            event_type="child_end",
            answer_preview="hello",
            tokens=2,
            tool_names=[],
            error=None,
        )

        await asyncio.wait_for(task, timeout=2.0)
        assert [e["type"] for e in received] == ["child_token", "child_end"]
        assert received[0]["token"] == "hello"
        # Root session id is injected by publish_event, not by the caller.
        assert all(e["root_session_id"] == "s1" for e in received)

    @pytest.mark.asyncio
    async def test_should_isolateEvents_when_multipleSessions(self):
        """Two independent sessions must not see each other's events.
        This is the multi-tenant property of the bus — a cross-session
        leak here would mean dashboard rows for tenant A showing up in
        tenant B's stream."""
        await fleet_bus.register_session("sA")
        await fleet_bus.register_session("sB")
        seen_a: list[dict] = []
        seen_b: list[dict] = []

        async def read_into(target: list, session: str):
            async for ev in fleet_bus.subscribe(session):
                target.append(ev)
                if ev["type"] == "child_end":
                    return

        ta = asyncio.create_task(read_into(seen_a, "sA"))
        tb = asyncio.create_task(read_into(seen_b, "sB"))
        await asyncio.sleep(0)

        fleet_bus.publish_event(
            root_session_id="sA", child_session_id="ca", role="r",
            event_type="child_token", token="A1",
        )
        fleet_bus.publish_event(
            root_session_id="sB", child_session_id="cb", role="r",
            event_type="child_token", token="B1",
        )
        fleet_bus.publish_event(
            root_session_id="sA", child_session_id="ca", role="r",
            event_type="child_end", answer_preview="A1", tokens=1,
            tool_names=[], error=None,
        )
        fleet_bus.publish_event(
            root_session_id="sB", child_session_id="cb", role="r",
            event_type="child_end", answer_preview="B1", tokens=1,
            tool_names=[], error=None,
        )

        await asyncio.wait_for(asyncio.gather(ta, tb), timeout=2.0)
        # No cross-talk
        assert all(e["root_session_id"] == "sA" for e in seen_a)
        assert all(e["root_session_id"] == "sB" for e in seen_b)
        assert {e["token"] for e in seen_a if e["type"] == "child_token"} == {"A1"}
        assert {e["token"] for e in seen_b if e["type"] == "child_token"} == {"B1"}

    @pytest.mark.asyncio
    async def test_should_endSubscription_when_sessionUnregistered(self):
        """unregister_session drains subscribers cleanly. If this regresses,
        SSE streams will hang forever after the request finishes."""
        await fleet_bus.register_session("s1")

        finished = asyncio.Event()

        async def reader():
            async for _ in fleet_bus.subscribe("s1"):
                pass
            finished.set()

        task = asyncio.create_task(reader())
        await asyncio.sleep(0)
        await fleet_bus.unregister_session("s1")
        await asyncio.wait_for(finished.wait(), timeout=2.0)
        task.cancel()  # cleanup
        # State cleaned up.
        assert fleet_bus._session_count() == 0

    @pytest.mark.asyncio
    async def test_should_recordCancel_when_requested(self):
        """request_cancel + is_cancelled wire up correctly."""
        await fleet_bus.register_session("s1")
        assert not fleet_bus.is_cancelled(
            root_session_id="s1", child_session_id="c1",
        )
        assert fleet_bus.request_cancel(
            root_session_id="s1", child_session_id="c1",
        )
        assert fleet_bus.is_cancelled(
            root_session_id="s1", child_session_id="c1",
        )
        # Unknown child = not cancelled (the cancel was for a different child).
        assert not fleet_bus.is_cancelled(
            root_session_id="s1", child_session_id="c2",
        )

    @pytest.mark.asyncio
    async def test_should_returnFalse_when_cancelOnUnknownSession(self):
        """Cancel against a session that doesn't exist returns False
        rather than raising. Justification: the child may have already
        finished and the session torn down by the time the cancel HTTP
        request arrives. That's a race, not an error."""
        assert not fleet_bus.request_cancel(
            root_session_id="ghost", child_session_id="c1",
        )


# ── Streaming integration: subagent → bus → subscriber ─────────────────────
class TestSubagentStreaming:
    """spawn_subagent must publish child_start, child_token, child_tool_*,
    child_end events to the bus tagged with the root_session_id from the
    parent context. These are the events the dashboard and SSE stream
    will consume."""

    @pytest.mark.asyncio
    async def test_should_publishEvents_when_childStreamsTokens(
        self, root_ctx, monkeypatch
    ):
        from app.agent import subagent as subagent_mod

        async def _fake_stream(state, config, version):
            yield {
                "event": "on_chat_model_start",
                "data": {"input": {"messages": [[_FakeMsg("the brief")]]}},
            }
            yield {"event": "on_tool_start", "name": "file_read",
                   "data": {"input": {"path": "x.py"}}}
            yield {"event": "on_tool_end", "name": "file_read",
                   "data": {"output": "file contents"}}
            yield {"event": "on_chat_model_stream",
                   "data": {"chunk": _FakeChunk("hello ")}}
            yield {"event": "on_chat_model_stream",
                   "data": {"chunk": _FakeChunk("world")}}

        fake_agent = MagicMock()
        fake_agent.astream_events = _fake_stream
        monkeypatch.setattr(
            subagent_mod, "_build_child_agent", lambda **kw: fake_agent
        )

        # Subscribe BEFORE the spawn so we catch the child_start event.
        await fleet_bus.register_session(root_ctx.root_session_id)
        received: list[dict] = []

        async def reader():
            async for ev in fleet_bus.subscribe(root_ctx.root_session_id):
                received.append(ev)
                if ev["type"] in ("child_end", "child_cancelled"):
                    return

        reader_task = asyncio.create_task(reader())
        await asyncio.sleep(0)

        result = await subagent_mod.spawn_subagent(
            role="reader",
            brief="read x.py",
            allowed_tools=["file_read"],
            max_tool_calls=2,
            max_tokens=200,
        )
        await asyncio.wait_for(reader_task, timeout=2.0)

        # Happy path: child completed normally.
        assert result.error is None
        assert result.answer == "hello world"

        # Event types in order. We don't pin exact token-event content
        # because the chunk boundaries are an implementation detail.
        types = [e["type"] for e in received]
        assert types[0] == "child_start"
        assert "child_token" in types
        assert "child_tool_start" in types
        assert "child_tool_end" in types
        assert types[-1] == "child_end"

        # child_start carries the brief preview and depth.
        start_ev = next(e for e in received if e["type"] == "child_start")
        assert start_ev["depth"] == 1
        assert "read x.py" in start_ev["brief_preview"]

        # Token events carry the per-chunk content.
        tokens = [e["token"] for e in received if e["type"] == "child_token"]
        assert "".join(tokens) == "hello world"

        # Tool events carry the tool name.
        tool_starts = [e for e in received if e["type"] == "child_tool_start"]
        assert tool_starts[0]["tool"] == "file_read"

        # child_end summarizes.
        end_ev = next(e for e in received if e["type"] == "child_end")
        assert end_ev["error"] is None
        assert end_ev["tool_names"] == ["file_read"]
        assert end_ev["tokens"] > 0

    @pytest.mark.asyncio
    async def test_should_publishCancelled_when_cancelRequestedMidStream(
        self, root_ctx, monkeypatch
    ):
        """If a cancel flag is set before/during the stream, _run_child
        must break cooperatively and return a partial result with
        error='cancelled by parent', AND publish a child_cancelled event
        (not child_end) so subscribers can distinguish completion from
        abort."""
        from app.agent import subagent as subagent_mod

        # Set the cancel flag for ANY child id we're about to spawn.
        # Because the child id is random (sub-{uuid8}), we monkeypatch
        # uuid in the spawn module to give us a deterministic id.
        deterministic_id = "deadbeef"
        import uuid as uuid_mod
        class _FakeUUID:
            hex = deterministic_id + "0000000000000000000000000000"
        monkeypatch.setattr(
            subagent_mod.uuid, "uuid4", lambda: _FakeUUID()
        )

        cancel_after = asyncio.Event()

        async def _slow_stream(state, config, version):
            # First yield a tool_start so something is captured, then
            # wait until the test sets the cancel flag, then yield more.
            yield {"event": "on_tool_start", "name": "file_read",
                   "data": {"input": {}}}
            await cancel_after.wait()
            # After the cancel is set, yield more events — _run_child
            # should bail out at the top of the loop on the NEXT iteration.
            yield {"event": "on_chat_model_stream",
                   "data": {"chunk": _FakeChunk("late")}}
            yield {"event": "on_chat_model_stream",
                   "data": {"chunk": _FakeChunk("er")}}

        fake_agent = MagicMock()
        fake_agent.astream_events = _slow_stream
        monkeypatch.setattr(
            subagent_mod, "_build_child_agent", lambda **kw: fake_agent
        )

        await fleet_bus.register_session(root_ctx.root_session_id)
        received: list[dict] = []

        async def reader():
            async for ev in fleet_bus.subscribe(root_ctx.root_session_id):
                received.append(ev)
                if ev["type"] in ("child_end", "child_cancelled"):
                    return

        reader_task = asyncio.create_task(reader())

        # Schedule the cancel + unblock the stream after a short delay,
        # giving spawn_subagent time to publish child_start.
        async def trigger_cancel():
            await asyncio.sleep(0.05)
            expected_child = f"{root_ctx.root_session_id}/sub-{deterministic_id}"
            fleet_bus.request_cancel(
                root_session_id=root_ctx.root_session_id,
                child_session_id=expected_child,
            )
            cancel_after.set()

        cancel_task = asyncio.create_task(trigger_cancel())

        result = await subagent_mod.spawn_subagent(
            role="reader",
            brief="should be cancelled",
            allowed_tools=["file_read"],
            max_tool_calls=2,
            max_tokens=200,
        )
        await asyncio.wait_for(reader_task, timeout=3.0)
        await cancel_task

        # Result carries the cancellation reason.
        assert result.error == "cancelled by parent"

        # Last event is child_cancelled, NOT child_end. This distinction
        # matters for the dashboard — cancelled spawns should be visually
        # different from completed ones.
        assert received[-1]["type"] == "child_cancelled"
        assert received[-1]["error"] == "cancelled by parent"

    @pytest.mark.asyncio
    async def test_should_workSilently_when_noBusRegistered(
        self, root_ctx, monkeypatch
    ):
        """Spawning into a session with no bus registered (e.g. eval
        harness, unit tests that don't care about streaming) must still
        produce a valid result. This is the back-compat property — the
        majority of existing tests don't register a bus."""
        from app.agent import subagent as subagent_mod

        async def _fake_stream(state, config, version):
            yield {"event": "on_chat_model_stream",
                   "data": {"chunk": _FakeChunk("ok")}}

        fake_agent = MagicMock()
        fake_agent.astream_events = _fake_stream
        monkeypatch.setattr(
            subagent_mod, "_build_child_agent", lambda **kw: fake_agent
        )

        # Deliberately NOT registering a bus for root_ctx.root_session_id.
        result = await subagent_mod.spawn_subagent(
            role="reader",
            brief="hi",
            allowed_tools=["file_read"],
            max_tool_calls=1,
            max_tokens=100,
        )
        assert result.error is None
        assert result.answer == "ok"
        # Bus was never registered → still zero sessions.
        assert fleet_bus._session_count() == 0
