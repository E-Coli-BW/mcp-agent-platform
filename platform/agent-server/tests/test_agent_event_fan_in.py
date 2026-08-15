"""Regression tests for draining parent and child agent event streams."""

from __future__ import annotations

import asyncio

import pytest

from app.agent.delegation import progress_bus as fleet_bus
from app.runtime.agent_event_fan_in import AgentEventFanIn


@pytest.fixture(autouse=True)
def reset_fleet_bus():
    fleet_bus._reset_for_tests()
    yield
    fleet_bus._reset_for_tests()


@pytest.mark.asyncio
async def test_drains_buffered_child_event_after_parent_completion():
    allow_parent_finish = asyncio.Event()

    async def parent_events():
        yield {"event": "parent_token"}
        await allow_parent_finish.wait()

    fan_in = AgentEventFanIn(
        agent_events=parent_events(),
        root_session_id="root",
    )
    await fan_in.start()
    try:
        source, event = await fan_in.next_event(timeout=1)
        assert source == "agent"
        assert event["event"] == "parent_token"

        allow_parent_finish.set()
        await asyncio.sleep(0)
        fleet_bus.publish_event(
            root_session_id="root",
            child_session_id="root/child-1",
            role="reader",
            event_type="child_token",
            token="buffered child output",
        )

        source, event = await fan_in.next_event(timeout=1)
        assert source == "bus"
        assert event["type"] == "child_token"
        assert event["token"] == "buffered child output"

        with pytest.raises(StopAsyncIteration):
            await fan_in.next_event(timeout=1)
    finally:
        await fan_in.close()


@pytest.mark.asyncio
async def test_installs_bus_subscriber_before_parent_can_publish_child_event():
    subscriber_counts: list[int] = []

    async def parent_events():
        subscriber_counts.append(fleet_bus._subscriber_count("root"))
        fleet_bus.publish_event(
            root_session_id="root",
            child_session_id="root/child-1",
            role="reader",
            event_type="child_start",
            depth=1,
            brief_preview="early child",
        )
        yield {"event": "parent_done"}

    fan_in = AgentEventFanIn(
        agent_events=parent_events(),
        root_session_id="root",
    )
    await fan_in.start()
    seen_sources: list[str] = []
    try:
        while True:
            try:
                source, event = await fan_in.next_event(timeout=1)
            except StopAsyncIteration:
                break
            seen_sources.append(source)
            if source == "bus":
                assert event["type"] == "child_start"
    finally:
        await fan_in.close()

    assert subscriber_counts == [1]
    assert "bus" in seen_sources
    assert fleet_bus._session_count() == 0


@pytest.mark.asyncio
async def test_close_is_safe_when_registration_fails():
    async def parent_events():
        yield {"event": "never_started"}

    async def fail_registration(_root_session_id: str) -> None:
        raise RuntimeError("registration unavailable")

    fan_in = AgentEventFanIn(
        agent_events=parent_events(),
        root_session_id="root",
        register_session=fail_registration,
    )

    with pytest.raises(RuntimeError, match="registration unavailable"):
        await fan_in.start()

    await fan_in.close()
    assert fleet_bus._session_count() == 0