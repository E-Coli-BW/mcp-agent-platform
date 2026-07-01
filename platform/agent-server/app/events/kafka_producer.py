"""Kafka event producer — publishes tool execution events for audit and analytics.

WHY KAFKA?
- Decouples the agent from downstream consumers (audit log, analytics, alerts)
- Events are durable — survives consumer downtime, can replay
- Multiple consumers can independently process the same events
- Enables cross-service analytics without coupling services

GRACEFUL DEGRADATION:
- If Kafka is unavailable, events are silently dropped (agent still works)
- Same pattern as our Redis conversation store: try/except → no-op on failure
"""

import json
import logging
import time
import uuid
from dataclasses import dataclass, asdict
from typing import Any

from app.events.model_provenance import validate_model_call_event

logger = logging.getLogger(__name__)

TOOL_EVENTS_TOPIC = "agent.tool.events"
AGENT_RESPONSES_TOPIC = "agent.responses"
MODEL_CALLS_TOPIC = "agent.model.calls"


@dataclass
class ToolEvent:
    """Schema for tool execution events published to Kafka."""
    event_id: str
    timestamp: str
    session_id: str
    event_type: str  # tool_start, tool_end, agent_response
    tool_name: str = ""
    tool_input: dict | None = None
    tool_output: str = ""
    model: str = ""
    duration_ms: int = 0
    token_count: int = 0

    def to_json(self) -> bytes:
        d = asdict(self)
        if d["tool_input"] is None:
            d["tool_input"] = {}
        return json.dumps(d, ensure_ascii=False, default=str).encode("utf-8")


class KafkaEventProducer:
    """Async Kafka producer for agent events.
    
    Lazy-initializes on first use. Falls back to no-op if Kafka is unavailable.
    """

    def __init__(self, bootstrap_servers: str = "localhost:9093"):
        self._bootstrap_servers = bootstrap_servers
        self._producer = None
        self._available = None
        self._last_retry: float = 0
        self._retry_interval: float = 30  # retry connection every 30s

    async def _get_producer(self):
        import time
        now = time.monotonic()
        # If previously failed, retry after interval (not permanent failure)
        if self._available is False:
            if (now - self._last_retry) < self._retry_interval:
                return None
            # Cooldown elapsed — try again
            self._available = None
            self._producer = None
        if self._producer is not None:
            return self._producer
        try:
            from aiokafka import AIOKafkaProducer
            self._producer = AIOKafkaProducer(
                bootstrap_servers=self._bootstrap_servers,
                value_serializer=lambda v: v,
                request_timeout_ms=2000,
            )
            await self._producer.start()
            self._available = True
            logger.info("Kafka producer connected to %s", self._bootstrap_servers)
            return self._producer
        except Exception as e:
            logger.debug("Kafka unavailable (will retry in %ds): %s", self._retry_interval, e)
            self._available = False
            self._last_retry = now
            return None

    async def _send(self, topic: str, event: ToolEvent):
        producer = await self._get_producer()
        if producer is None:
            return
        try:
            await producer.send(topic, event.to_json(), key=event.session_id.encode("utf-8"))
        except Exception as e:
            logger.debug("Kafka send failed (dropping event): %s", e)

    async def _send_json(self, topic: str, payload: dict[str, Any], key: str | None = None):
        producer = await self._get_producer()
        if producer is None:
            return
        try:
            data = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
            key_bytes = key.encode("utf-8") if key else None
            await producer.send(topic, data, key=key_bytes)
        except Exception as e:
            logger.debug("Kafka send failed (dropping event): %s", e)

    async def emit_tool_start(self, session_id: str, tool_name: str,
                               tool_input: dict, model: str = ""):
        event = ToolEvent(
            event_id=str(uuid.uuid4()),
            timestamp=time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
            session_id=session_id, event_type="tool_start",
            tool_name=tool_name, tool_input=tool_input, model=model,
        )
        await self._send(TOOL_EVENTS_TOPIC, event)

    async def emit_tool_end(self, session_id: str, tool_name: str,
                             tool_output: str, duration_ms: int = 0):
        event = ToolEvent(
            event_id=str(uuid.uuid4()),
            timestamp=time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
            session_id=session_id, event_type="tool_end",
            tool_name=tool_name, tool_output=tool_output[:500],
            duration_ms=duration_ms,
        )
        await self._send(TOOL_EVENTS_TOPIC, event)

    async def emit_agent_response(self, session_id: str, model: str,
                                    token_count: int, duration_ms: int):
        event = ToolEvent(
            event_id=str(uuid.uuid4()),
            timestamp=time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
            session_id=session_id, event_type="agent_response",
            model=model, token_count=token_count, duration_ms=duration_ms,
        )
        await self._send(AGENT_RESPONSES_TOPIC, event)

    async def emit_model_call(self, payload: dict[str, Any]):
        is_valid, error = validate_model_call_event(payload)
        if not is_valid:
            logger.warning(
                "Dropping invalid model-call provenance event [event_id=%s]: %s",
                payload.get("event_id", "unknown"),
                error,
            )
            return
        session_id = str(payload.get("session_id", ""))
        await self._send_json(MODEL_CALLS_TOPIC, payload, key=session_id if session_id else None)

    async def close(self):
        if self._producer:
            try:
                await self._producer.flush()
                await self._producer.stop()
            except Exception:
                pass


_producer = None


def get_event_producer() -> KafkaEventProducer:
    global _producer
    if _producer is None:
        from app.config import settings
        bootstrap = getattr(settings, "kafka_bootstrap_servers", "localhost:9093")
        _producer = KafkaEventProducer(bootstrap_servers=bootstrap)
    return _producer
