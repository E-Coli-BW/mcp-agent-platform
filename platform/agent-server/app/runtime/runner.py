"""Transport-neutral orchestration for one platform Agent turn."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from app.agent.engine import AgentEngine, AgentTurnRequest
from app.agent.prompts import PromptResolution, resolve_system_prompt
from app.config import settings
from app.context.request_context import get_request_context, set_prompt_version
from app.runtime import RunStatus, get_run_recorder
from app.runtime.agent_event_fan_in import AgentEventFanIn
from app.runtime.agent_output import (
    FinalResponseBuffer,
    ToolEventTracker,
    is_actor_model_event,
    verified_artifact_answer_from_chain_event,
)
from app.runtime.cancellation import (
    RunCancellationRegistry,
    get_run_cancellation_registry,
)
from app.runtime.events import RuntimeStreamEvent
from app.runtime.observers.base import ObserverDispatcher, TurnObserverContext
from app.runtime.policy import get_policy_evaluator
from app.runtime.recorder import RunRecorder
from app.runtime.tool_authorization import (
    reset_approval_notifier,
    set_approval_notifier,
)
from app.runtime.tool_invocation import make_tool_invocation_envelope
from app.store.conversation import Message, get_conversation_store

logger = logging.getLogger("agent.runtime")

DisconnectionCheck = Callable[[], Awaitable[bool]]
ObserverFactory = Callable[[TurnObserverContext, RunRecorder], ObserverDispatcher]
PromptResolver = Callable[..., PromptResolution]


@dataclass(frozen=True, slots=True)
class RuntimeTurnRequest:
    model: str
    session_id: str
    tenant_id: str
    user_input: str
    temperature: float | None = None
    max_tokens: int | None = None
    prompt_version: str | None = None
    is_disconnected: DisconnectionCheck | None = None


class SessionLane(Protocol):
    async def is_held(self, session_id: str) -> bool: ...

    async def acquire(self, session_id: str, max_wait: float) -> bool: ...

    async def release(self, session_id: str) -> None: ...


class DefaultSessionLane:
    async def is_held(self, session_id: str) -> bool:
        from app.middleware.session_lane import session_lock_held

        return await session_lock_held(session_id)

    async def acquire(self, session_id: str, max_wait: float) -> bool:
        from app.middleware.session_lane import wait_for_session_lock

        return await wait_for_session_lock(session_id, max_wait=max_wait)

    async def release(self, session_id: str) -> None:
        from app.middleware.session_lane import release_session_lock

        await release_session_lock(session_id)


class RunCoordinator:
    """Wrap Agent execution with durable platform lifecycle capabilities."""

    def __init__(
        self,
        engine: AgentEngine,
        *,
        observer_factory: ObserverFactory | None = None,
        prompt_resolver: PromptResolver = resolve_system_prompt,
        conversation_store: Any | None = None,
        recorder: RunRecorder | None = None,
        cancellation: RunCancellationRegistry | None = None,
        session_lane: SessionLane | None = None,
        fan_in_factory: Callable[..., AgentEventFanIn] = AgentEventFanIn,
    ) -> None:
        self._engine = engine
        self._observer_factory = observer_factory
        self._prompt_resolver = prompt_resolver
        self._conversation_store = conversation_store or get_conversation_store()
        self._recorder = recorder or get_run_recorder()
        self._cancellation = cancellation or get_run_cancellation_registry()
        self._session_lane = session_lane or DefaultSessionLane()
        self._fan_in_factory = fan_in_factory

    async def stream_turn(
        self,
        request: RuntimeTurnRequest,
    ) -> AsyncIterator[RuntimeStreamEvent]:
        prompt = self._prompt_resolver(
            requested_version=request.prompt_version,
            tenant_id=request.tenant_id,
            session_id=request.session_id,
        )
        set_prompt_version(prompt.version)
        agent_events = await self._engine.stream_turn(
            AgentTurnRequest(
                model=request.model,
                session_id=request.session_id,
                tenant_id=request.tenant_id,
                user_input=request.user_input,
                recursion_limit=settings.max_agent_steps * 4,
                temperature=request.temperature,
                prompt_version=prompt.version,
            )
        )

        request_context = get_request_context()
        runtime_name = "python-v2"
        effective_temperature = (
            request.temperature
            if request.temperature is not None
            else settings.default_temperature
        )
        observer_context = TurnObserverContext(
            run_id=request_context.run_id,
            request_id=request_context.request_id,
            tenant_id=request.tenant_id,
            session_id=request.session_id,
            model=request.model,
            runtime=runtime_name,
            temperature=effective_temperature,
            max_tokens=request.max_tokens,
            prompt_id=prompt.prompt_id,
            prompt_version=prompt.version,
            prompt_hash=prompt.content_hash,
            feature_flags={},
        )
        observers = self._create_observers(observer_context)

        await self._conversation_store.append(
            request.session_id,
            Message(role="user", content=request.user_input),
        )
        input_preview = (
            request.user_input[:120] + "..."
            if len(request.user_input) > 120
            else request.user_input
        )
        logger.info(
            "Received user message [session=%s, model=%s]: %s",
            request.session_id,
            request.model,
            input_preview,
        )

        full_response = ""
        lock_acquired = False
        client_disconnected = False
        cancel_requested = False
        fan_in: AgentEventFanIn | None = None
        approval_notifier_token = None
        response_buffer = FinalResponseBuffer()
        tool_events = ToolEventTracker()
        start_time = time.time()

        try:
            await self._recorder.start_run(
                run_id=request_context.run_id,
                request_id=request_context.request_id,
                tenant_id=request.tenant_id,
                session_id=request.session_id,
                model=request.model,
                metadata={
                    "runtime": runtime_name,
                    "prompt_version": prompt.version,
                    "prompt_hash": prompt.content_hash,
                    "temperature": effective_temperature,
                },
            )
            await self._cancellation.register(request_context.run_id)
            status_event = await self._recorder.record_event(
                request_context.run_id,
                "status_thinking",
                {"request_id": request_context.request_id},
            )
            yield RuntimeStreamEvent(
                "status",
                {
                    "state": "thinking",
                    "request_id": request_context.request_id,
                    "run_id": request_context.run_id,
                },
                event_id=status_event.event_id,
            )

            test_delay_ms = int(os.environ.get("AGENT_TEST_DELAY_MS", "0"))
            if test_delay_ms > 0:
                await asyncio.sleep(test_delay_ms / 1000)

            if await self._session_lane.is_held(request.session_id):
                yield RuntimeStreamEvent(
                    "status",
                    {
                        "state": "queued",
                        "request_id": request_context.request_id,
                        "run_id": request_context.run_id,
                        "message": "Your message is queued behind the active turn",
                    },
                )

            if not await self._session_lane.acquire(request.session_id, max_wait=900):
                await self._recorder.finish_run(
                    request_context.run_id,
                    RunStatus.FAILED,
                    {
                        "request_id": request_context.request_id,
                        "session_id": request.session_id,
                        "terminal_reason": "session_lane_timeout",
                        "retryable": True,
                    },
                )
                yield RuntimeStreamEvent(
                    "content",
                    {
                        "content": (
                            "⚠️ Another request for this session is still processing. "
                            "Please wait."
                        ),
                        "model": request.model,
                        "finish_reason": "stop",
                    },
                )
                yield RuntimeStreamEvent("done")
                return
            lock_acquired = True

            fan_in = self._fan_in_factory(
                agent_events=agent_events,
                root_session_id=request.session_id,
            )

            async def notify_approval(approval: Any) -> None:
                await fan_in.emit("approval_required", approval)

            approval_notifier_token = set_approval_notifier(notify_approval)

            try:
                await fan_in.start()
                while True:
                    if (
                        request.is_disconnected is not None
                        and await request.is_disconnected()
                    ):
                        logger.info(
                            "Client disconnected [session=%s]",
                            request.session_id,
                        )
                        client_disconnected = True
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
                        cancel_requested = True
                        await self._recorder.record_event(
                            request_context.run_id,
                            "cancel_requested",
                            {
                                "request_id": request_context.request_id,
                                "session_id": request.session_id,
                            },
                        )
                        await fan_in.close()
                        break

                    try:
                        source, event = await fan_in.next_event(timeout=15)
                    except TimeoutError:
                        yield RuntimeStreamEvent("keepalive")
                        continue
                    except StopAsyncIteration:
                        break

                    if source == "agent_error":
                        raise event
                    if source == "approval_required":
                        yield RuntimeStreamEvent(
                            "approval_required",
                            {"approval": event},
                        )
                        continue
                    if source == "bus":
                        yield RuntimeStreamEvent("child_event", {"event": event})
                        continue

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
            finally:
                await fan_in.close()
                if approval_notifier_token is not None:
                    reset_approval_notifier(approval_notifier_token)

            await observers.observe("stream_finished")
            if not client_disconnected and not cancel_requested:
                full_response = response_buffer.text
                if full_response:
                    yield RuntimeStreamEvent(
                        "content",
                        {"content": full_response, "model": request.model},
                    )

            if full_response:
                await self._conversation_store.append(
                    request.session_id,
                    Message(role="assistant", content=full_response),
                )

            duration_ms = int((time.time() - start_time) * 1000)
            await observers.observe(
                "turn_finished",
                {"duration_ms": duration_ms, "response": full_response},
            )
            report = observers.report
            tool_count = tool_events.count
            response_preview = (
                full_response[:120] + "..."
                if len(full_response) > 120
                else full_response
            )
            logger.info(
                "Agent response [session=%s, tools=%d, %dms]: %s",
                request.session_id,
                tool_count,
                duration_ms,
                response_preview.replace("\n", " "),
            )

            run_finished_event = await self._recorder.finish_run(
                request_context.run_id,
                (
                    RunStatus.CANCELLED
                    if client_disconnected or cancel_requested
                    else RunStatus.COMPLETED
                ),
                {
                    "request_id": request_context.request_id,
                    "session_id": request.session_id,
                    "tool_count": tool_count,
                    "duration_ms": duration_ms,
                    "prompt_tokens": report.prompt_tokens,
                    "completion_tokens": report.completion_tokens,
                    "terminal_reason": (
                        "client_disconnected"
                        if client_disconnected
                        else "cancel_requested"
                        if cancel_requested
                        else "completed"
                    ),
                    "retryable": bool(client_disconnected or cancel_requested),
                },
            )
            yield RuntimeStreamEvent(
                "status",
                {
                    "state": (
                        "cancelled"
                        if client_disconnected or cancel_requested
                        else "complete"
                    ),
                    "request_id": request_context.request_id,
                    "run_id": request_context.run_id,
                    "tool_count": tool_count,
                    "duration_ms": duration_ms,
                    "retrieval_trace_ids": list(report.retrieval_trace_ids),
                },
                event_id=run_finished_event.event_id,
            )
            yield RuntimeStreamEvent(
                "content",
                {"content": "", "model": request.model, "finish_reason": "stop"},
            )
            yield RuntimeStreamEvent("done")

        except Exception as error:
            logger.exception("Agent turn failed [run=%s]", request_context.run_id)
            await observers.observe("turn_failed", {"error": error})
            terminal_reason = getattr(error, "terminal_reason", "agent_error")
            retryable = bool(
                getattr(
                    error,
                    "retryable",
                    not isinstance(error, (ValueError, PermissionError, KeyError)),
                )
            )
            failure_payload = {
                "request_id": request_context.request_id,
                "session_id": request.session_id,
                "error_class": error.__class__.__name__,
                "error_message": str(error),
                "terminal_reason": terminal_reason,
                "retryable": retryable,
            }
            if bool(getattr(error, "preserve_run", False)):
                await self._recorder.record_event(
                    request_context.run_id,
                    "run_ownership_transferred",
                    failure_payload,
                )
            else:
                await self._recorder.finish_run(
                    request_context.run_id,
                    RunStatus.FAILED,
                    failure_payload,
                )
            yield RuntimeStreamEvent(
                "content",
                {
                    "content": f"\n\n❌ Agent error: {error}",
                    "model": request.model,
                    "finish_reason": "stop",
                },
            )
            yield RuntimeStreamEvent("done")
        finally:
            if lock_acquired:
                await self._session_lane.release(request.session_id)
            await self._cancellation.unregister(request_context.run_id)

    def _create_observers(
        self,
        context: TurnObserverContext,
    ) -> ObserverDispatcher:
        if self._observer_factory is None:
            return ObserverDispatcher(context)
        try:
            return self._observer_factory(context, self._recorder)
        except Exception:
            logger.exception(
                "Runtime observer composition failed [run=%s]",
                context.run_id,
            )
            return ObserverDispatcher(context)

    async def _handle_tool_start(
        self,
        event: dict[str, Any],
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
        event: dict[str, Any],
        run_id: str,
        tool_events: ToolEventTracker,
        observers: ObserverDispatcher,
    ) -> AsyncIterator[RuntimeStreamEvent]:
        tool_sequence = tool_events.end(event)
        if tool_sequence is None:
            return
        tool_name = event.get("name", "unknown")
        tool_output = str(event.get("data", {}).get("output", ""))[:500]
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
            },
        )
        yield RuntimeStreamEvent(
            "tool_end",
            {
                "tool": tool_name,
                "data": {"input": tool_input, "output": tool_output[:200]},
                "seq": tool_sequence,
            },
            event_id=recorded.event_id,
        )