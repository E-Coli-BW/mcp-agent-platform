"""Fan-in lifecycle for parent agent and child fleet-bus events."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from app.agent import fleet_bus

logger = logging.getLogger(__name__)

AgentEvent = dict[str, Any]
BusEvent = dict[str, Any]
RegisterSession = Callable[[str], Awaitable[None]]
UnregisterSession = Callable[[str], Awaitable[None]]
Subscribe = Callable[[str, asyncio.Event | None], AsyncIterator[BusEvent]]


class AgentEventFanIn:
    """Merge parent events with child events without dropping buffered output.

    The bus subscriber is installed before the parent starts, so early child
    events cannot publish into an empty subscriber set. Parent completion closes
    the bus input, then the fan-in drains every event already queued before it
    reports completion to the caller.
    """

    def __init__(
        self,
        *,
        agent_events: AsyncIterator[AgentEvent],
        root_session_id: str,
        register_session: RegisterSession = fleet_bus.register_session,
        unregister_session: UnregisterSession = fleet_bus.unregister_session,
        subscribe: Subscribe = fleet_bus.subscribe,
    ) -> None:
        self._agent_events = agent_events
        self._root_session_id = root_session_id
        self._register_session = register_session
        self._unregister_session = unregister_session
        self._subscribe = subscribe
        self._queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
        self._bus_ready = asyncio.Event()
        self._agent_task: asyncio.Task[None] | None = None
        self._bus_task: asyncio.Task[None] | None = None
        self._registered = False
        self._agent_done = False
        self._bus_done = False
        self._closed = False

    async def start(self) -> None:
        """Register and subscribe before beginning the parent event stream."""
        if self._agent_task is not None:
            return
        await self._register_session(self._root_session_id)
        self._registered = True
        self._bus_task = asyncio.create_task(self._drain_bus())
        await self._bus_ready.wait()
        if self._closed:
            return
        self._agent_task = asyncio.create_task(self._drain_agent())

    async def next_event(self, timeout: float) -> tuple[str, Any]:
        """Return the next visible event or stop after both sources are drained."""
        while True:
            source, event = await asyncio.wait_for(self._queue.get(), timeout=timeout)
            if source == "agent_done":
                self._agent_done = True
                await self._unregister_once()
                if self._bus_done:
                    raise StopAsyncIteration
                continue
            if source == "bus_done":
                self._bus_done = True
                if self._agent_done:
                    raise StopAsyncIteration
                continue
            return source, event

    async def emit(self, source: str, event: Any) -> None:
        """Inject an external event, such as an approval notification."""
        if not self._closed:
            await self._queue.put((source, event))

    async def close(self) -> None:
        """Cancel unfinished producers and release the fleet session exactly once."""
        if self._closed:
            return
        self._closed = True
        await self._unregister_once()
        for task in (self._agent_task, self._bus_task):
            if task is not None and not task.done():
                task.cancel()
        for task in (self._agent_task, self._bus_task):
            if task is not None:
                try:
                    await task
                except asyncio.CancelledError:
                    logger.debug("Fan-in producer cancelled [session=%s]", self._root_session_id)
                except Exception:
                    logger.exception("Fan-in producer failed while closing [session=%s]", self._root_session_id)

    async def _drain_agent(self) -> None:
        try:
            async for event in self._agent_events:
                await self._queue.put(("agent", event))
        except Exception as exc:
            logger.exception("Agent event stream failed [session=%s]", self._root_session_id)
            await self._queue.put(("agent_error", exc))
        finally:
            await self._queue.put(("agent_done", None))

    async def _drain_bus(self) -> None:
        try:
            async for event in self._subscribe(self._root_session_id, self._bus_ready):
                await self._queue.put(("bus", event))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Fleet bus subscription failed for session=%s", self._root_session_id)
        finally:
            self._bus_ready.set()
            await self._queue.put(("bus_done", None))

    async def _unregister_once(self) -> None:
        if not self._registered:
            return
        self._registered = False
        await self._unregister_session(self._root_session_id)