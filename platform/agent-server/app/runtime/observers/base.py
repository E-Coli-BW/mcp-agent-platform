"""Observer contracts for optional platform capabilities around Agent runs."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class TurnObserverContext:
    run_id: str
    request_id: str
    tenant_id: str
    session_id: str
    model: str
    runtime: str
    temperature: float
    max_tokens: int | None
    prompt_id: str
    prompt_version: str
    prompt_hash: str
    feature_flags: dict[str, Any]


@dataclass(slots=True)
class TurnObservationReport:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    tool_count: int = 0
    duration_ms: int = 0
    retrieval_trace_ids: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class RuntimeObservation:
    kind: str
    payload: dict[str, Any] = field(default_factory=dict)
    raw_event: dict[str, Any] | None = None


class RuntimeObserver(Protocol):
    async def observe(self, observation: RuntimeObservation) -> None: ...


class ObserverDispatcher:
    """Fan out observations without letting optional observers own execution."""

    def __init__(
        self,
        context: TurnObserverContext,
        observers: list[RuntimeObserver] | None = None,
    ) -> None:
        self.context = context
        self.report = TurnObservationReport()
        self._observers = list(observers or [])

    def add(self, *observers: RuntimeObserver) -> None:
        self._observers.extend(observers)

    async def observe(
        self,
        kind: str,
        payload: dict[str, Any] | None = None,
        raw_event: dict[str, Any] | None = None,
    ) -> None:
        observation = RuntimeObservation(
            kind=kind,
            payload=payload or {},
            raw_event=raw_event,
        )
        for observer in self._observers:
            try:
                await observer.observe(observation)
            except Exception:
                logger.exception(
                    "Runtime observer failed [observer=%s, event=%s, run=%s]",
                    type(observer).__name__,
                    kind,
                    self.context.run_id,
                )