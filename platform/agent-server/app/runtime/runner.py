"""Transport-neutral orchestration for one platform Agent turn."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol
from uuid import uuid4

from app.agent.engine import AgentEngine, AgentTurnRequest
from app.agent.execution_policy.system_prompts import (
    PromptResolution,
    resolve_system_prompt,
)
from app.config import settings
from app.context.request_context import (
    RequestContext,
    get_request_context,
    set_prompt_version,
)
from app.runtime import RunStatus, get_run_recorder
from app.runtime.agent_event_fan_in import AgentEventFanIn
from app.runtime.agent_output import (
    FinalResponseBuffer,
    ToolEventTracker,
)
from app.runtime.cancellation import (
    RunCancellationRegistry,
    get_run_cancellation_registry,
)
from app.runtime.events import RuntimeStreamEvent
from app.runtime.observers.base import ObserverDispatcher, TurnObserverContext
from app.runtime.recorder import RunRecorder
from app.runtime.turn_events import (
    AgentEventFanInFactory,
    TurnEventStreamer,
    TurnTermination,
)
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

    async def acquire(
        self, session_id: str, max_wait: float, owner_token: str
    ) -> bool: ...

    async def release(self, session_id: str, owner_token: str) -> None: ...

    async def maintain(self, session_id: str, owner_token: str) -> None: ...


class ConversationHistory(Protocol):
    """The conversation-history operations required by one runtime turn."""

    async def append(self, session_id: str, message: Message) -> None: ...


class DefaultSessionLane:
    async def is_held(self, session_id: str) -> bool:
        from app.middleware.session_lane import session_lock_held

        return await session_lock_held(session_id)

    async def acquire(
        self, session_id: str, max_wait: float, owner_token: str
    ) -> bool:
        from app.middleware.session_lane import wait_for_session_lock

        return await wait_for_session_lock(
            session_id, owner_token=owner_token, max_wait=max_wait
        )

    async def release(self, session_id: str, owner_token: str) -> None:
        from app.middleware.session_lane import release_session_lock

        await release_session_lock(session_id, owner_token)

    async def maintain(self, session_id: str, owner_token: str) -> None:
        from app.middleware.session_lane import maintain_session_lock

        await maintain_session_lock(session_id, owner_token)


class RunCoordinator:
    """Coordinate Agent execution with platform run-lifecycle capabilities."""

    def __init__(
        self,
        engine: AgentEngine,
        *,
        observer_factory: ObserverFactory | None = None,
        prompt_resolver: PromptResolver = resolve_system_prompt,
        conversation_store: ConversationHistory | None = None,
        recorder: RunRecorder | None = None,
        cancellation: RunCancellationRegistry | None = None,
        session_lane: SessionLane | None = None,
        fan_in_factory: AgentEventFanInFactory = AgentEventFanIn,
    ) -> None:
        self._engine = engine
        self._observer_factory = observer_factory
        self._prompt_resolver = prompt_resolver
        self._conversation_store = (
            conversation_store
            if conversation_store is not None
            else get_conversation_store()
        )
        self._recorder = recorder if recorder is not None else get_run_recorder()
        self._cancellation = (
            cancellation
            if cancellation is not None
            else get_run_cancellation_registry()
        )
        self._event_streamer = TurnEventStreamer(self._recorder, self._cancellation)
        self._session_lane = session_lane if session_lane is not None else DefaultSessionLane()
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
        from app.tools.agent_mode import get_workspace_root

        workspace_root = get_workspace_root()
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
            "Received user message [session=%s, model=%s, workspace=%s]: %s",
            request.session_id,
            request.model,
            workspace_root,
            input_preview,
        )

        lock_acquired = False
        lock_renewal_task: asyncio.Task | None = None
        lock_owner_token = uuid4().hex
        termination = TurnTermination()
        response_buffer = FinalResponseBuffer(max_chars=settings.max_response_chars)
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
                    "workspace_root": workspace_root,
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

            if not await self._session_lane.acquire(
                request.session_id,
                max_wait=900,
                owner_token=lock_owner_token,
            ):
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
            lock_renewal_task = asyncio.create_task(
                self._session_lane.maintain(request.session_id, lock_owner_token)
            )

            fan_in = self._fan_in_factory(
                agent_events=agent_events,
                root_session_id=request.session_id,
            )
            async for visible_event in self._event_streamer.stream(
                fan_in=fan_in,
                request=request,
                request_context=request_context,
                response_buffer=response_buffer,
                tool_events=tool_events,
                observers=observers,
                termination=termination,
            ):
                yield visible_event

            async for event in self._finish_turn(
                request=request,
                request_context=request_context,
                response_buffer=response_buffer,
                tool_events=tool_events,
                observers=observers,
                termination=termination,
                started_at=start_time,
            ):
                yield event

        except Exception as error:  # noqa: BLE001 - convert turn failures to stream events
            async for event in self._fail_turn(
                request=request,
                request_context=request_context,
                observers=observers,
                error=error,
            ):
                yield event
        finally:
            if lock_renewal_task is not None:
                lock_renewal_task.cancel()
                try:
                    await lock_renewal_task
                except asyncio.CancelledError:
                    pass
            if lock_acquired:
                await self._session_lane.release(
                    request.session_id, owner_token=lock_owner_token
                )
            await self._cancellation.unregister(request_context.run_id)

    async def _finish_turn(
        self,
        *,
        request: RuntimeTurnRequest,
        request_context: RequestContext,
        response_buffer: FinalResponseBuffer,
        tool_events: ToolEventTracker,
        observers: ObserverDispatcher,
        termination: TurnTermination,
        started_at: float,
    ) -> AsyncIterator[RuntimeStreamEvent]:
        """Persist a completed or cancelled turn and emit its terminal events."""
        full_response = ""
        await observers.observe("stream_finished")
        was_cancelled = termination.client_disconnected or termination.cancel_requested
        if not was_cancelled:
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

        duration_ms = int((time.time() - started_at) * 1000)
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
            RunStatus.CANCELLED if was_cancelled else RunStatus.COMPLETED,
            {
                "request_id": request_context.request_id,
                "session_id": request.session_id,
                "tool_count": tool_count,
                "duration_ms": duration_ms,
                "prompt_tokens": report.prompt_tokens,
                "completion_tokens": report.completion_tokens,
                "terminal_reason": self._terminal_reason(termination),
                "retryable": was_cancelled,
            },
        )
        yield RuntimeStreamEvent(
            "status",
            {
                "state": "cancelled" if was_cancelled else "complete",
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

    @staticmethod
    def _terminal_reason(termination: TurnTermination) -> str:
        if termination.client_disconnected:
            return "client_disconnected"
        if termination.cancel_requested:
            return "cancel_requested"
        return "completed"

    async def _fail_turn(
        self,
        *,
        request: RuntimeTurnRequest,
        request_context: RequestContext,
        observers: ObserverDispatcher,
        error: Exception,
    ) -> AsyncIterator[RuntimeStreamEvent]:
        """Record a failed turn and emit the error and terminal events."""
        logger.error(
            "Agent turn failed [run=%s]",
            request_context.run_id,
            exc_info=(type(error), error, error.__traceback__),
        )
        await observers.observe("turn_failed", {"error": error})
        failure_payload = {
            "request_id": request_context.request_id,
            "session_id": request.session_id,
            "error_class": error.__class__.__name__,
            "error_message": str(error),
            "terminal_reason": getattr(error, "terminal_reason", "agent_error"),
            "retryable": bool(
                getattr(
                    error,
                    "retryable",
                    not isinstance(error, (ValueError, PermissionError, KeyError)),
                )
            ),
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
