"""Process-local cooperative cancellation for active root runs."""

from __future__ import annotations

import asyncio


class RunCancellationRegistry:
    """Bounded-by-lifecycle cancellation signals for active runs.

    Signals are deliberately process-local in this milestone. Durable or
    cross-process cancellation belongs to the runtime control plane.
    """

    def __init__(self) -> None:
        self._events: dict[str, asyncio.Event] = {}
        self._lock = asyncio.Lock()

    async def register(self, run_id: str) -> None:
        async with self._lock:
            self._events.setdefault(run_id, asyncio.Event())

    async def request(self, run_id: str) -> bool:
        async with self._lock:
            event = self._events.get(run_id)
            if event is None:
                return False
            event.set()
            return True

    async def is_requested(self, run_id: str) -> bool:
        async with self._lock:
            event = self._events.get(run_id)
            return event.is_set() if event is not None else False

    async def wait(self, run_id: str) -> bool:
        """Wait until an active run is cancelled without polling."""
        async with self._lock:
            event = self._events.get(run_id)
        if event is None:
            return False
        await event.wait()
        return True

    async def unregister(self, run_id: str) -> None:
        async with self._lock:
            self._events.pop(run_id, None)


_registry = RunCancellationRegistry()


def get_run_cancellation_registry() -> RunCancellationRegistry:
    return _registry


def reset_run_cancellation_registry() -> None:
    """Reset the singleton for isolated tests."""
    global _registry
    _registry = RunCancellationRegistry()
