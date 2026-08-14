"""Default platform observer composition."""

from __future__ import annotations

from app.runtime.observers.base import ObserverDispatcher, TurnObserverContext
from app.runtime.observers.kafka import KafkaObserver
from app.runtime.observers.provenance import ModelProvenanceObserver
from app.runtime.observers.retrieval import RetrievalFeedbackObserver
from app.runtime.observers.usage import UsageObserver
from app.runtime.recorder import RunRecorder


def create_default_observers(
    context: TurnObserverContext,
    recorder: RunRecorder,
) -> ObserverDispatcher:
    dispatcher = ObserverDispatcher(context)
    report = dispatcher.report
    dispatcher.add(
        UsageObserver(context, report),
        ModelProvenanceObserver(context, report, recorder),
        KafkaObserver(context, report),
        RetrievalFeedbackObserver(context, report),
    )
    return dispatcher