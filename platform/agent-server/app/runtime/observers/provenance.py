"""Model-call provenance observer."""

from __future__ import annotations

import hashlib
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


def _response_metadata(value: Any, depth: int = 0) -> dict[str, Any]:
    if value is None or depth > 4:
        return {}
    metadata = getattr(value, "response_metadata", None)
    if isinstance(metadata, dict) and metadata:
        return metadata
    if isinstance(value, dict):
        metadata = value.get("response_metadata")
        if isinstance(metadata, dict) and metadata:
            return metadata
        children = value.values()
    elif isinstance(value, (list, tuple)):
        children = value
    else:
        children = (
            getattr(value, name, None)
            for name in ("message", "messages", "generations", "output")
        )
    for child in children:
        metadata = _response_metadata(child, depth + 1)
        if metadata:
            return metadata
    return {}


def model_performance_from_event(event: dict[str, Any] | None) -> dict[str, Any]:
    """Extract Ollama-compatible nanosecond timing metadata from a model event."""
    if event is None:
        return {}
    output = event.get("data", {}).get("output")
    metadata = _response_metadata(output)
    if not metadata:
        return {}

    performance: dict[str, Any] = {}
    duration_fields = {
        "total_duration": "total_duration_ms",
        "load_duration": "load_duration_ms",
        "prompt_eval_duration": "prefill_duration_ms",
        "eval_duration": "decode_duration_ms",
    }
    for source, target in duration_fields.items():
        value = metadata.get(source)
        if isinstance(value, (int, float)) and value >= 0:
            performance[target] = int(value / 1_000_000)

    count_fields = {
        "prompt_eval_count": "prefill_tokens",
        "eval_count": "decode_tokens",
    }
    for source, target in count_fields.items():
        value = metadata.get(source)
        if isinstance(value, (int, float)) and value >= 0:
            performance[target] = int(value)

    for prefix in ("prefill", "decode"):
        tokens = performance.get(f"{prefix}_tokens")
        duration_ms = performance.get(f"{prefix}_duration_ms")
        if isinstance(tokens, int) and isinstance(duration_ms, int) and duration_ms > 0:
            performance[f"{prefix}_tokens_per_second"] = round(
                tokens * 1000 / duration_ms,
                2,
            )
    prefill_duration_ms = performance.get("prefill_duration_ms")
    if isinstance(prefill_duration_ms, int):
        load_duration_ms = performance.get("load_duration_ms", 0)
        performance["ttft_ms"] = prefill_duration_ms + load_duration_ms
        performance["ttft_estimated"] = True
    return performance


def prompt_prefix_fingerprint(
    event: dict[str, Any] | None,
) -> tuple[str | None, int]:
    """Fingerprint the stable leading system message used for prefix caching."""
    if event is None:
        return None, 0
    model_input = event.get("data", {}).get("input", {})
    messages = model_input.get("messages", []) if isinstance(model_input, dict) else []
    if messages and isinstance(messages[0], list):
        messages = messages[0]
    if not messages:
        return None, 0
    content = getattr(messages[0], "content", None)
    if not isinstance(content, str):
        return None, 0
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return f"sha256:{digest}", len(content)


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
            chunk = (observation.raw_event or {}).get("data", {}).get("chunk")
            content = getattr(chunk, "content", None)
            if content and self._active["first_token_at"] is None:
                self._active["first_token_at"] = time.monotonic()
            self._active["completion_tokens"] += completion_tokens_from_event(
                observation.raw_event
            )
        elif observation.kind == "model_completed":
            await self._complete("ok", raw_event=observation.raw_event)
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
        input_prefix_hash, input_prefix_chars = prompt_prefix_fingerprint(event)
        self._active = {
            "seq": self._sequence,
            "started_at": time.monotonic(),
            "prompt_tokens": prompt_tokens,
            "completion_tokens": 0,
            "first_token_at": None,
            "input_prefix_hash": input_prefix_hash,
            "input_prefix_chars": input_prefix_chars,
        }
        await self._recorder.record_event(
            self._context.run_id,
            "model_call_started",
            {
                "seq": self._sequence,
                "provider": infer_provider(self._context.model),
                "model": self._context.model,
                "prompt_tokens": prompt_tokens,
                "input_prefix_hash": input_prefix_hash,
                "input_prefix_chars": input_prefix_chars,
                "prompt_version": self._context.prompt_version,
                "prompt_hash": self._context.prompt_hash,
            },
        )

    async def _complete(
        self,
        status: str,
        error_class: str | None = None,
        raw_event: dict[str, Any] | None = None,
    ) -> None:
        if self._active is None:
            return
        active = self._active
        duration_ms = int((time.monotonic() - active["started_at"]) * 1000)
        performance = model_performance_from_event(raw_event)
        if active["first_token_at"] is not None:
            performance["ttft_ms"] = int(
                (active["first_token_at"] - active["started_at"]) * 1000
            )
            performance["ttft_estimated"] = False
        prompt_tokens = performance.get("prefill_tokens", active["prompt_tokens"])
        completion_tokens = performance.get(
            "decode_tokens",
            active["completion_tokens"],
        )
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
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            duration_ms=duration_ms,
            status=status,
            error_class=error_class,
            performance=performance,
            input_prefix_hash=active["input_prefix_hash"],
            input_prefix_chars=active["input_prefix_chars"],
        )
        await self._producer.emit_model_call(event)
        payload = {
            "seq": active["seq"],
            "provider": infer_provider(self._context.model),
            "model": self._context.model,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "duration_ms": duration_ms,
            "status": status,
            "input_prefix_hash": active["input_prefix_hash"],
            "input_prefix_chars": active["input_prefix_chars"],
        }
        if performance:
            payload["performance"] = performance
        if error_class is not None:
            payload["error_class"] = error_class
        await self._recorder.record_event(
            self._context.run_id,
            "model_call_completed",
            payload,
        )
        self._active = None