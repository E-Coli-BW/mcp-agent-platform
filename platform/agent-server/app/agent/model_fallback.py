"""Compatibility exports for the model fallback owner."""

from app.agent.model.fallback import (
    _CB_COOLDOWN,
    _CB_THRESHOLD,
    ChatModelWithFallback,
    CircuitBreaker,
    _breakers,
    _get_breaker,
)

__all__ = [
    "_CB_COOLDOWN",
    "_CB_THRESHOLD",
    "ChatModelWithFallback",
    "CircuitBreaker",
    "_breakers",
    "_get_breaker",
]
