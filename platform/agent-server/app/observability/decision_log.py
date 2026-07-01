"""Observability — structured decision logging and metrics for the agent system.

Three layers:
  1. Tracing: OpenTelemetry spans for every graph node (latency, token counts)
  2. Decision Logs: Structured events for compression, skill activation, fact extraction
  3. Session Replay: State snapshots at key mutation points

This module provides the decision logging layer. OTel integration is opt-in
via the OTEL_ENABLED env var. Session replay uses the same structured log format
but emits full state snapshots.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


# ── Event Types ───────────────────────────────────────────────────────────────


class EventType(str, Enum):
    COMPRESSION = "compression"
    SKILL_ACTIVATION = "skill_activation"
    FACT_EXTRACTION = "fact_extraction"
    STATE_SNAPSHOT = "state_snapshot"
    TOKEN_BUDGET = "token_budget"
    ERROR_STREAK = "error_streak"


# ── Decision Events ──────────────────────────────────────────────────────────


@dataclass
class CompressionEvent:
    """Emitted when context compression fires."""
    session_id: str
    turn_number: int
    trigger: str  # "token_budget_exceeded" | "explicit_request"
    before_tokens: int
    after_tokens: int
    messages_dropped: int
    messages_summarized: int
    facts_retained: list[str] = field(default_factory=list)
    compression_ratio: float = 0.0

    def __post_init__(self):
        if self.before_tokens > 0:
            self.compression_ratio = self.after_tokens / self.before_tokens


@dataclass
class SkillActivationEvent:
    """Emitted when a skill is auto-activated (Layer 2 or 3)."""
    session_id: str
    turn_number: int
    layer: int  # 2 = error-triggered, 3 = proactive
    skill_key: str
    match_score: float
    match_reason: str  # e.g. "regex match on ClassNotFoundException"
    was_useful: bool | None = None  # filled in later via feedback


@dataclass
class FactExtractionEvent:
    """Emitted when the investigation state is updated."""
    session_id: str
    turn_number: int
    new_facts: list[str] = field(default_factory=list)
    new_eliminations: list[str] = field(default_factory=list)
    hypothesis_changed: bool = False


@dataclass
class TokenBudgetEvent:
    """Emitted every turn for token usage tracking."""
    session_id: str
    turn_number: int
    total_tokens: int
    budget_tokens: int
    usage_pct: float = 0.0

    def __post_init__(self):
        if self.budget_tokens > 0:
            self.usage_pct = self.total_tokens / self.budget_tokens


@dataclass
class StateSnapshot:
    """Full state snapshot at a key mutation point."""
    session_id: str
    turn_number: int
    event: str  # "pre_compression", "post_compression", "skill_activated", "task_complete"
    message_count: int
    token_estimate: int
    investigation_summary: str = ""
    timestamp: float = field(default_factory=time.time)


# ── Decision Logger ──────────────────────────────────────────────────────────


class DecisionLogger:
    """Structured decision logger — writes events for post-hoc analysis.

    In production, these events go to Kafka topics. In dev/test,
    they're written to structured log lines (JSON) for easy grep.

    Usage:
        decision_logger.log_compression(CompressionEvent(...))
        decision_logger.log_skill_activation(SkillActivationEvent(...))
    """

    def __init__(self, kafka_enabled: bool = False, kafka_producer=None):
        self._kafka_enabled = kafka_enabled
        self._producer = kafka_producer
        self._event_buffer: list[dict[str, Any]] = []

    def log_compression(self, event: CompressionEvent) -> None:
        self._emit(EventType.COMPRESSION, asdict(event))

    def log_skill_activation(self, event: SkillActivationEvent) -> None:
        self._emit(EventType.SKILL_ACTIVATION, asdict(event))

    def log_fact_extraction(self, event: FactExtractionEvent) -> None:
        self._emit(EventType.FACT_EXTRACTION, asdict(event))

    def log_token_budget(self, event: TokenBudgetEvent) -> None:
        self._emit(EventType.TOKEN_BUDGET, asdict(event))

    def log_snapshot(self, event: StateSnapshot) -> None:
        self._emit(EventType.STATE_SNAPSHOT, asdict(event))

    def get_buffer(self) -> list[dict[str, Any]]:
        """Get buffered events (for testing)."""
        return list(self._event_buffer)

    def clear_buffer(self) -> None:
        self._event_buffer.clear()

    def _emit(self, event_type: EventType, data: dict[str, Any]) -> None:
        """Emit event to appropriate sink."""
        envelope = {
            "event_type": event_type.value,
            "timestamp": time.time(),
            "data": data,
        }

        # Always buffer (for testing and in-process access)
        self._event_buffer.append(envelope)

        # Structured log (always, for log aggregation)
        logger.info(
            "📊 [%s] %s",
            event_type.value,
            json.dumps(data, default=str, ensure_ascii=False),
        )

        # Kafka (production)
        if self._kafka_enabled and self._producer:
            topic = f"agent.decisions.{event_type.value}"
            try:
                self._producer.send(topic, value=envelope)
            except Exception as e:
                logger.warning("Failed to emit to Kafka topic %s: %s", topic, e)


# ── Singleton ─────────────────────────────────────────────────────────────────

_logger_instance: DecisionLogger | None = None


def get_decision_logger() -> DecisionLogger:
    """Get or create the singleton decision logger."""
    global _logger_instance
    if _logger_instance is None:
        _logger_instance = DecisionLogger()
    return _logger_instance


# ── Metrics (Prometheus-compatible counters) ──────────────────────────────────


class AgentMetrics:
    """Simple counter-based metrics. Exportable to Prometheus via /metrics endpoint."""

    def __init__(self):
        self.compressions_total: int = 0
        self.skill_activations_total: int = 0
        self.skill_activations_by_layer: dict[int, int] = {2: 0, 3: 0}
        self.facts_extracted_total: int = 0
        self.token_budget_exceeded_total: int = 0
        self.redundant_rework_detected: int = 0

    def record_compression(self, ratio: float) -> None:
        self.compressions_total += 1

    def record_skill_activation(self, layer: int) -> None:
        self.skill_activations_total += 1
        self.skill_activations_by_layer[layer] = (
            self.skill_activations_by_layer.get(layer, 0) + 1
        )

    def record_fact_extraction(self, count: int) -> None:
        self.facts_extracted_total += count

    def record_budget_exceeded(self) -> None:
        self.token_budget_exceeded_total += 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "compressions_total": self.compressions_total,
            "skill_activations_total": self.skill_activations_total,
            "skill_activations_by_layer": self.skill_activations_by_layer,
            "facts_extracted_total": self.facts_extracted_total,
            "token_budget_exceeded_total": self.token_budget_exceeded_total,
            "redundant_rework_detected": self.redundant_rework_detected,
        }


_metrics_instance: AgentMetrics | None = None


def get_agent_metrics() -> AgentMetrics:
    global _metrics_instance
    if _metrics_instance is None:
        _metrics_instance = AgentMetrics()
    return _metrics_instance
