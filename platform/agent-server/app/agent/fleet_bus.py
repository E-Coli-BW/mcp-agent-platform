"""FleetBus — per-root-session in-memory pub/sub for subagent fleet events.

WHY this exists
---------------
When a parent agent calls ``spawn_subagent``, the parent's ReAct loop is
blocked inside the tool call until the child returns. From the parent's
``astream_events`` perspective, ``spawn_subagent`` is opaque — no progress
visible, no cancel possible, no early-stop.

The FleetBus turns that opaque box into a streaming sub-stream:

- ``spawn_subagent`` (in subagent.py) publishes ``child_start``,
  ``child_token``, ``child_tool_*``, and ``child_end`` events to the bus,
  scoped to the root session.
- ``chat.py``'s ``_stream_agent_response`` subscribes to the bus for the
  duration of the parent's stream and forwards bus events to the browser
  as named SSE events (``event: child_token`` etc.).
- A cancel HTTP endpoint flips a per-child ``asyncio.Event`` on the bus;
  ``_run_child`` polls it between LangGraph events and exits cooperatively
  with ``error="cancelled by parent"``.

WHY a ContextVar + dict (not Redis / Kafka)
-------------------------------------------
The bus is intentionally **in-process**. The parent and the child run in
the same FastAPI worker — there is no network boundary to cross. Pulling
in Redis Pub/Sub here would buy us zero correctness, add a hard runtime
dependency, and create new failure modes (Redis down → fleet cancel
broken). When/if subagents move out-of-process the bus interface stays
the same and only the backing store changes.

WHY ``asyncio.Queue`` per subscriber
------------------------------------
Multiple parent streams could subscribe to the same root_session_id (e.g.
the chat SSE stream PLUS the dashboard's tail endpoint). Each subscriber
needs its own queue or one slow consumer will block all of them. Bounded
queue (``maxsize=256``) prevents a runaway child from OOMing the bus —
we drop oldest with a warning instead of blocking the publisher.

WHY events are plain dicts
--------------------------
The wire format on the parent SSE side is already JSON. Forcing a
TypedDict / dataclass here just creates conversion at the edge. Schema is
documented in this module's ``EVENT_TYPES`` constant — that's the
contract.

Lifecycle
---------
- ``register_session(root_id)`` is called by chat.py at request start.
- ``unregister_session(root_id)`` MUST be called in chat.py's finally —
  otherwise pending children publish into a dead session forever and the
  bus grows unbounded.
- A bus for an unknown root_id silently drops publishes (defensive: a
  late-arriving child event after request teardown shouldn't crash the
  worker).
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Optional


logger = logging.getLogger(__name__)


# ── Event schema (documented, not enforced) ────────────────────────────────
# Every event dict has at minimum:
#   - "type":               one of EVENT_TYPES
#   - "root_session_id":    populated by publish_event()
#   - "child_session_id":   the spawn this event came from
#   - "role":               the child's role label (for UI grouping)
#
# Additional fields per event type:
#   child_start:     {"depth": int, "brief_preview": str}
#   child_token:     {"token": str}
#   child_tool_start:{"tool": str, "input_preview": str}
#   child_tool_end:  {"tool": str, "output_preview": str}
#   child_end:       {"answer_preview": str, "tokens": int, "duration_ms": int,
#                     "tool_names": list[str], "error": Optional[str]}
#   child_cancelled: {"reason": str}
#   child_verified:  {"verified": bool, "grade": Optional[int],
#                     "reasoning": str, "retried": bool}  ← C3
EVENT_TYPES = frozenset({
    "child_start",
    "child_token",
    "child_tool_start",
    "child_tool_end",
    "child_end",
    "child_cancelled",
    "child_verified",
})


# Maximum events queued per subscriber. Tuned for ~5-second buffer at
# typical streaming rates (~50 tokens/sec). If a subscriber is slower than
# that, we drop oldest and log — better than blocking the publisher
# (which would block the child agent's stream).
_MAX_QUEUE_SIZE = 256


@dataclass
class _SessionState:
    """Per-root-session bus state.

    Subscribers: each subscriber gets its own queue so a slow consumer
    doesn't starve faster ones.

    Cancel events: keyed by child_session_id so a parent can target a
    specific child. Auto-created on first request to ``is_cancelled`` /
    ``request_cancel`` so callers don't have to pre-register children.
    """

    subscribers: list[asyncio.Queue] = field(default_factory=list)
    cancel_events: dict[str, asyncio.Event] = field(default_factory=dict)


# Global bus state. Keyed by root_session_id. Single shared dict because
# the bus is process-local and there's exactly one event loop per worker.
# Access is single-threaded under asyncio so no lock needed for
# dict-mutation; we only need a lock when adding/removing subscribers
# concurrently from different tasks.
_sessions: dict[str, _SessionState] = {}
_sessions_lock = asyncio.Lock()


# ── Lifecycle ──────────────────────────────────────────────────────────────
async def register_session(root_session_id: str) -> None:
    """Create the bus state for a new root request.

    Idempotent — calling twice with the same id is a no-op. Tests rely
    on this so they can register without checking first.
    """
    async with _sessions_lock:
        if root_session_id not in _sessions:
            _sessions[root_session_id] = _SessionState()
            logger.debug("🚌 fleet_bus: registered session=%s", root_session_id)


async def unregister_session(root_session_id: str) -> None:
    """Tear down the bus state for a finished root request.

    Drains subscriber queues with a sentinel ``None`` so any subscriber
    iterators exit cleanly. Idempotent.
    """
    async with _sessions_lock:
        state = _sessions.pop(root_session_id, None)
    if state is None:
        return
    # Wake any subscribers so they exit their async for loop.
    for q in state.subscribers:
        try:
            q.put_nowait(None)
        except asyncio.QueueFull:
            # Subscriber already over-buffered; force-drain one and retry.
            try:
                q.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                q.put_nowait(None)
            except asyncio.QueueFull:
                pass  # give up; subscriber will eventually GC
    logger.debug("🚌 fleet_bus: unregistered session=%s", root_session_id)


# ── Publish ────────────────────────────────────────────────────────────────
def publish_event(
    *,
    root_session_id: str,
    child_session_id: str,
    role: str,
    event_type: str,
    **payload: Any,
) -> None:
    """Publish an event to every subscriber on ``root_session_id``.

    Non-blocking: if a subscriber's queue is full we drop the OLDEST event
    in that queue and put the new one. The alternative — blocking the
    publisher — would cascade into the child agent's stream and
    eventually time out a useful spawn.

    No-op if no bus is registered for this session. This lets
    ``spawn_subagent`` be called from non-streaming contexts (unit tests,
    eval harness) without forcing bus setup.
    """
    if event_type not in EVENT_TYPES:
        # Caller bug — fail loud in tests but don't crash in prod.
        logger.warning("🚌 fleet_bus: unknown event_type=%s", event_type)

    state = _sessions.get(root_session_id)
    if state is None:
        return  # not registered — silently drop

    event = {
        "type": event_type,
        "root_session_id": root_session_id,
        "child_session_id": child_session_id,
        "role": role,
        **payload,
    }

    for q in state.subscribers:
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            # Drop oldest to make room. We deliberately don't log on every
            # drop — a runaway child could spam millions of warnings. The
            # subscriber will see fewer events than the publisher sent;
            # the dashboard already shows the final SubagentResult as
            # ground truth so partial event loss is recoverable.
            try:
                q.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass  # give up on this event


# ── Subscribe ──────────────────────────────────────────────────────────────
async def subscribe(root_session_id: str) -> AsyncIterator[dict]:
    """Async iterator over events for ``root_session_id``.

    Auto-registers the session if not already registered. Caller is
    responsible for tearing down the subscription by exiting the async
    for loop (e.g. when their parent SSE stream ends).

    The iterator terminates when either:
      - ``unregister_session`` is called (sentinel ``None`` enqueued), or
      - the caller's task is cancelled (async iteration breaks naturally).

    Yields plain dicts with the event schema documented at module top.
    """
    await register_session(root_session_id)
    q: asyncio.Queue = asyncio.Queue(maxsize=_MAX_QUEUE_SIZE)
    async with _sessions_lock:
        state = _sessions[root_session_id]
        state.subscribers.append(q)
    try:
        while True:
            item = await q.get()
            if item is None:  # unregister sentinel
                return
            yield item
    finally:
        async with _sessions_lock:
            state = _sessions.get(root_session_id)
            if state is not None and q in state.subscribers:
                state.subscribers.remove(q)


# ── Cancellation ───────────────────────────────────────────────────────────
def request_cancel(
    *, root_session_id: str, child_session_id: str
) -> bool:
    """Flip the cancel flag for ``child_session_id``.

    Returns True if the cancel was recorded, False if the root session
    isn't registered (in which case the child has likely already
    finished — cancel is a no-op).

    The actual cancellation happens cooperatively in ``_run_child``,
    which polls ``is_cancelled`` between LangGraph events. A child
    blocked inside a long-running tool call will NOT be interrupted
    until that tool returns; this is a known limitation documented in
    the subagent module.
    """
    state = _sessions.get(root_session_id)
    if state is None:
        return False
    ev = state.cancel_events.get(child_session_id)
    if ev is None:
        ev = asyncio.Event()
        state.cancel_events[child_session_id] = ev
    ev.set()
    logger.info(
        "🛑 fleet_bus: cancel requested for child=%s in session=%s",
        child_session_id, root_session_id,
    )
    return True


def is_cancelled(*, root_session_id: str, child_session_id: str) -> bool:
    """True if ``request_cancel`` has been called for this child.

    Idempotent — polling is the intended usage pattern from
    ``_run_child``'s event loop.
    """
    state = _sessions.get(root_session_id)
    if state is None:
        return False
    ev = state.cancel_events.get(child_session_id)
    return ev is not None and ev.is_set()


# ── Test helpers ───────────────────────────────────────────────────────────
def _reset_for_tests() -> None:
    """Drop all bus state. Tests should call this in a fixture teardown
    to avoid cross-test leakage."""
    _sessions.clear()


def _session_count() -> int:
    """Number of currently-registered sessions. For test assertions."""
    return len(_sessions)


def _subscriber_count(root_session_id: str) -> int:
    """Number of subscribers on a session. For test assertions."""
    state = _sessions.get(root_session_id)
    return len(state.subscribers) if state else 0
