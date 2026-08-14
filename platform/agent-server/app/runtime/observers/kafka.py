"""Kafka audit and response observer."""

from __future__ import annotations

from app.events.kafka_producer import KafkaEventProducer, get_event_producer
from app.runtime.observers.base import (
    RuntimeObservation,
    TurnObservationReport,
    TurnObserverContext,
)


class KafkaObserver:
    def __init__(
        self,
        context: TurnObserverContext,
        report: TurnObservationReport,
        producer: KafkaEventProducer | None = None,
    ) -> None:
        self._context = context
        self._report = report
        self._producer = producer or get_event_producer()

    async def observe(self, observation: RuntimeObservation) -> None:
        payload = observation.payload
        if observation.kind == "tool_started":
            await self._producer.emit_tool_start(
                self._context.session_id,
                str(payload["tool"]),
                dict(payload["input"]),
                self._context.model,
            )
        elif observation.kind == "tool_completed":
            await self._producer.emit_tool_end(
                self._context.session_id,
                str(payload["tool"]),
                str(payload["output"]),
            )
        elif observation.kind == "turn_finished":
            await self._producer.emit_agent_response(
                self._context.session_id,
                self._context.model,
                self._report.prompt_tokens + self._report.completion_tokens,
                int(payload["duration_ms"]),
            )