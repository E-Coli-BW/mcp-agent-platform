"""Default composition for platform runtime capabilities."""

from __future__ import annotations

from dataclasses import replace

from app.agent.engine import get_agent_engine
from app.events.model_provenance import build_feature_flags_snapshot
from app.runtime.observers.base import ObserverDispatcher, TurnObserverContext
from app.runtime.observers.default import create_default_observers
from app.runtime.recorder import RunRecorder
from app.runtime.runner import RunCoordinator


def _default_observer_factory(
    context: TurnObserverContext,
    recorder: RunRecorder,
) -> ObserverDispatcher:
    context = replace(context, feature_flags=build_feature_flags_snapshot())
    return create_default_observers(context, recorder)


def get_run_coordinator() -> RunCoordinator:
    """Compose the default Engine and optional platform observers."""
    return RunCoordinator(
        get_agent_engine(),
        observer_factory=_default_observer_factory,
    )