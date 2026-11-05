"""Translate Agent events into lifecycle and client-visible runtime events."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from app.agent.engine import AgentEvent
from app.agent.tool_routing.required_tools import tool_message_is_error
from app.config import settings
from app.context.request_context import RequestContext
from app.runtime.agent_output import (
    FinalResponseBuffer,
    ToolEventTracker,
    is_actor_model_event,
    tool_call_progress_from_model_event,
    verified_artifact_answer_from_chain_event,
)
from app.runtime.cancellation import RunCancellationRegistry
from app.runtime.events import RuntimeStreamEvent
from app.runtime.observers.base import ObserverDispatcher
from app.runtime.policy import get_policy_evaluator
from app.runtime.recorder import RunRecorder
from app.runtime.tool_authorization import reset_approval_notifier, set_approval_notifier
from app.runtime.tool_invocation import make_tool_invocation_envelope

if TYPE_CHECKING:
    from app.runtime.runner import RuntimeTurnRequest

logger = logging.getLogger("agent.runtime.events")


@dataclass(slots=True)
class TurnTermination:
    client_disconnected: bool = False
    cancel_requested: bool = False


class AgentEventStream(Protocol):
    """Operations the runtime needs from a merged agent event stream."""

    async def start(self) -> None: ...

    async def next_event(self, timeout: float) -> tuple[str, Any]: ...

    async def emit(self, source: str, event: Any) -> None: ...

    async def close(self) -> None: ...


class AgentEventFanInFactory(Protocol):
    """Construct the event merger used to observe one agent turn."""

    def __call__(
        self,
        *,
        agent_events: AsyncIterator[AgentEvent],
        root_session_id: str,
    ) -> AgentEventStream: ...


class TurnEventStreamer:
    """Own event consumption, lifecycle projection, and per-event handling."""

    def __init__(
        self,
        recorder: RunRecorder,
        cancellation: RunCancellationRegistry,
    ) -> None:
        self._recorder = recorder
        self._cancellation = cancellation

    async def stream(
        self,
        *,
        fan_in: AgentEventStream,
        request: RuntimeTurnRequest,
        request_context: RequestContext,
        response_buffer: FinalResponseBuffer,
        tool_events: ToolEventTracker,
        observers: ObserverDispatcher,
        termination: TurnTermination,
    ) -> AsyncIterator[RuntimeStreamEvent]:
        async def notify_approval(approval: Any) -> None:
            await fan_in.emit("approval_required", approval)

        approval_notifier_token = set_approval_notifier(notify_approval)
        try:
            await fan_in.start()
            while True:
                if request.is_disconnected is not None and await request.is_disconnected():
                    logger.info("Client disconnected [session=%s]", request.session_id)
                    termination.client_disconnected = True
                    await self._recorder.record_event(
                        request_context.run_id,
                        "client_disconnected",
                        {"session_id": request.session_id},
                    )
                    await fan_in.close()
                    break

                if await self._cancellation.is_requested(request_context.run_id):
                    logger.info(
                        "Root run cancellation requested [run=%s]",
                        request_context.run_id,
                    )
                    termination.cancel_requested = True
                    await self._record_cancellation(request, request_context)
                    await fan_in.close()
                    break

                try:
                    next_event = await self._next_event_or_cancel(
                        fan_in,
                        request_context.run_id,
                    )
                    if next_event is None:
                        logger.info(
                            "Root run cancellation requested [run=%s]",
                            request_context.run_id,
                        )
                        termination.cancel_requested = True
                        await self._record_cancellation(request, request_context)
                        await fan_in.close()
                        break
                    source, event = next_event
                except TimeoutError:
                    yield RuntimeStreamEvent("keepalive")
                    continue
                except StopAsyncIteration:
                    break

                if source == "agent_error":
                    raise event
                if source == "approval_required":
                    yield RuntimeStreamEvent("approval_required", {"approval": event})
                    continue
                if source == "bus":
                    yield RuntimeStreamEvent("child_event", {"event": event})
                    continue

                async for visible_event in self._project_agent_event(
                    event,
                    request,
                    request_context,
                    response_buffer,
                    tool_events,
                    observers,
                ):
                    yield visible_event
        finally:
            await fan_in.close()
            reset_approval_notifier(approval_notifier_token)

    async def _record_cancellation(
        self,
        request: RuntimeTurnRequest,
        request_context: RequestContext,
    ) -> None:
        await self._recorder.record_event(
            request_context.run_id,
            "cancel_requested",
            {
                "request_id": request_context.request_id,
                "session_id": request.session_id,
            },
        )

    async def _project_agent_event(
        self,
        event: AgentEvent,
        request: RuntimeTurnRequest,
        request_context: RequestContext,
        response_buffer: FinalResponseBuffer,
        tool_events: ToolEventTracker,
        observers: ObserverDispatcher,
    ) -> AsyncIterator[RuntimeStreamEvent]:
        """Handle one LangGraph event from the root Agent."""
        kind = event.get("event", "")
        artifact_answer = verified_artifact_answer_from_chain_event(event)
        if artifact_answer:
            response_buffer.start_call()
            response_buffer.append(artifact_answer)

        if kind == "on_chat_model_start":
            if is_actor_model_event(event):
                response_buffer.start_call()
            await observers.observe("model_started", raw_event=event)
        elif kind == "on_chat_model_stream":
            chunk = event.get("data", {}).get("chunk")
            content = getattr(chunk, "content", None)
            if content and is_actor_model_event(event):
                response_buffer.append(content)
            await observers.observe("model_streamed", raw_event=event)
        elif kind == "on_chat_model_error":
            await observers.observe(
                "model_failed",
                {"error": event.get("data", {}).get("error")},
                raw_event=event,
            )
        elif kind == "on_chat_model_end":
            await observers.observe("model_completed", raw_event=event)
            if (
                settings.tool_call_progress_mode == "tool_call_text"
                and is_actor_model_event(event)
            ):
                progress = tool_call_progress_from_model_event(event)
                if progress:
                    progress_event = await self._recorder.record_event(
                        request_context.run_id,
                        "status_progress",
                        {"state": "working", "message": progress},
                    )
                    yield RuntimeStreamEvent(
                        "status",
                        {"state": "working", "message": progress},
                        event_id=progress_event.event_id,
                    )
        elif kind == "on_tool_start":
            async for visible_event in self._handle_tool_start(
                event,
                request,
                request_context.run_id,
                request_context.request_id,
                tool_events,
                observers,
            ):
                yield visible_event
        elif kind == "on_tool_end":
            async for visible_event in self._handle_tool_end(
                event,
                request_context.run_id,
                tool_events,
                observers,
            ):
                yield visible_event

    async def _next_event_or_cancel(
        self,
        fan_in: AgentEventStream,
        run_id: str,
    ) -> tuple[str, Any] | None:
        event_task = asyncio.create_task(fan_in.next_event(timeout=15))
        cancel_task = asyncio.create_task(self._cancellation.wait(run_id))
        try:
            done, _ = await asyncio.wait(
                {event_task, cancel_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if cancel_task in done and cancel_task.result():
                return None
            return event_task.result()
        finally:
            for task in (event_task, cancel_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(event_task, cancel_task, return_exceptions=True)

    async def _handle_tool_start(
        self,
        event: AgentEvent,
        request: RuntimeTurnRequest,
        run_id: str,
        request_id: str,
        tool_events: ToolEventTracker,
        observers: ObserverDispatcher,
    ) -> AsyncIterator[RuntimeStreamEvent]:
        tool_sequence = tool_events.start(event)
        if tool_sequence is None:
            return
        tool_name = event.get("name", "unknown")
        tool_input = event.get("data", {}).get("input", {})
        normalized_input = (
            tool_input if isinstance(tool_input, dict) else {"input": tool_input}
        )
        invocation = make_tool_invocation_envelope(
            run_id=run_id,
            request_id=request_id,
            session_id=request.session_id,
            tenant_id=request.tenant_id,
            step_seq=tool_sequence,
            tool_name=tool_name,
            arguments=normalized_input,
        )
        policy_decision = get_policy_evaluator().evaluate(invocation)
        await observers.observe(
            "tool_started",
            {
                "seq": tool_sequence,
                "tool": tool_name,
                "input": normalized_input,
            },
            raw_event=event,
        )
        recorded = await self._recorder.record_event(
            run_id,
            "tool_started",
            {
                "seq": tool_sequence,
                "tool": tool_name,
                "input": tool_input,
                "invocation": invocation.to_event_payload(),
                "policy_decision": policy_decision.to_event_payload(),
            },
        )
        yield RuntimeStreamEvent(
            "tool_start",
            {
                "tool": tool_name,
                "data": {"input": tool_input},
                "seq": tool_sequence,
            },
            event_id=recorded.event_id,
        )

    async def _handle_tool_end(
        self,
        event: AgentEvent,
        run_id: str,
        tool_events: ToolEventTracker,
        observers: ObserverDispatcher,
    ) -> AsyncIterator[RuntimeStreamEvent]:
        tool_sequence = tool_events.end(event)
        if tool_sequence is None:
            return
        tool_name = event.get("name", "unknown")
        raw_tool_output = event.get("data", {}).get("output", "")
        tool_output = str(raw_tool_output)[:500]
        tool_status = "error" if tool_message_is_error(raw_tool_output) else "ok"
        tool_input = event.get("data", {}).get("input", {})
        normalized_input = (
            tool_input if isinstance(tool_input, dict) else {"input": tool_input}
        )
        await observers.observe(
            "tool_completed",
            {
                "seq": tool_sequence,
                "tool": tool_name,
                "input": normalized_input,
                "output": tool_output,
                "status": tool_status,
            },
            raw_event=event,
        )
        recorded = await self._recorder.record_event(
            run_id,
            "tool_completed",
            {
                "seq": tool_sequence,
                "tool": tool_name,
                "input": tool_input,
                "output_preview": tool_output[:200],
                "status": tool_status,
            },
        )
        yield RuntimeStreamEvent(
            "tool_end",
            {
                "tool": tool_name,
                "data": {
                    "input": tool_input,
                    "output": tool_output[:200],
                    "status": tool_status,
                },
                "seq": tool_sequence,
            },
            event_id=recorded.event_id,
        )
