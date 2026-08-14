"""Model-call provenance observer."""

from __future__ import annotations

import time
from typing import Any

from app.events.kafka_producer import KafkaEventProducer, get_event_producer
from app.events.model_provenance import (
    current_trace_id,
    infer_provider,
    make_model_call_event,
)
from app.runtime.observers.base import (
    RuntimeObservation,
    TurnObservationReport,
    TurnObserverContext,
)
from app.runtime.observers.usage import (
    completion_tokens_from_event,
    prompt_tokens_from_event,
)
from app.runtime.recorder import RunRecorder


class ModelProvenanceObserver:
    def __init__(
        self,
        context: TurnObserverContext,
        report: TurnObservationReport,
        recorder: RunRecorder,
        producer: KafkaEventProducer | None = None,
    ) -> None:
        self._context = context
        self._report = report
        self._recorder = recorder
        self._producer = producer or get_event_producer()
        self._sequence = 0
        self._active: dict[str, Any] | None = None

    async def observe(self, observation: RuntimeObservation) -> None:
        if observation.kind == "model_started":
            await self._start(observation.raw_event)
        elif observation.kind == "model_streamed" and self._active is not None:
            self._active["completion_tokens"] += completion_tokens_from_event(
                observation.raw_event
            )
        elif observation.kind == "model_completed":
            await self._complete("ok")
        elif observation.kind == "model_failed":
            error = observation.payload.get("error")
            error_class = type(error).__name__ if error is not None else "UnknownError"
            await self._complete("error", error_class)
        elif observation.kind == "stream_finished":
            await self._complete("ok")
        elif observation.kind == "turn_failed":
            error = observation.payload.get("error")
            await self._complete(
                "error",
                type(error).__name__ if error is not None else "UnknownError",
            )

    async def _start(self, event: dict[str, Any] | None) -> None:
        self._sequence += 1
        prompt_tokens = prompt_tokens_from_event(event)
        self._active = {
            "seq": self._sequence,
            "started_at": time.monotonic(),
            "prompt_tokens": prompt_tokens,
            "completion_tokens": 0,
        }
        await self._recorder.record_event(
            self._context.run_id,
            "model_call_started",
            {
                "seq": self._sequence,
                "provider": infer_provider(self._context.model),
                "model": self._context.model,
                "prompt_tokens": prompt_tokens,
                "prompt_version": self._context.prompt_version,
                "prompt_hash": self._context.prompt_hash,
            },
        )

    async def _complete(
        self,
        status: str,
        error_class: str | None = None,
    ) -> None:
        if self._active is None:
            return
        active = self._active
        duration_ms = int((time.monotonic() - active["started_at"]) * 1000)
        event = make_model_call_event(
            run_id=self._context.run_id,
            request_id=self._context.request_id,
            trace_id=current_trace_id(),
            tenant_id=self._context.tenant_id,
            session_id=self._context.session_id,
            runtime=self._context.runtime,
            call_site=f"chat._stream_agent_response.llm_call_{active['seq']}",
            provider=infer_provider(self._context.model),
            model=self._context.model,
            temperature=self._context.temperature,
            max_tokens=self._context.max_tokens,
            prompt_id=self._context.prompt_id,
            prompt_version=self._context.prompt_version,
            prompt_hash=self._context.prompt_hash,
            feature_flags=self._context.feature_flags,
            prompt_tokens=active["prompt_tokens"],
            completion_tokens=active["completion_tokens"],
            duration_ms=duration_ms,
            status=status,
            error_class=error_class,
        )
        await self._producer.emit_model_call(event)
        payload = {
            "seq": active["seq"],
            "provider": infer_provider(self._context.model),
            "model": self._context.model,
            "prompt_tokens": active["prompt_tokens"],
            "completion_tokens": active["completion_tokens"],
            "duration_ms": duration_ms,
            "status": status,
        }
        if error_class is not None:
            payload["error_class"] = error_class
        await self._recorder.record_event(
            self._context.run_id,
            "model_call_completed",
            payload,
        )
        self._active = None