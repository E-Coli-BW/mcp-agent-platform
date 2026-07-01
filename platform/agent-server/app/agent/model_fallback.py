"""LLM Fallback Chain — automatic failover between model providers.

Architecture:
    primary model → fallback model → circuit breaker (friendly error)

When the primary model fails (timeout, 429, 5xx, connection error), the
request is automatically retried on the fallback model. If both fail, a
friendly error is returned instead of a raw exception.

Circuit breaker: after N consecutive failures on a provider, it's marked
"open" for a cooldown period — subsequent requests skip it immediately
instead of waiting for timeout.

Configuration via env vars:
    AGENT_FALLBACK_MODEL: fallback model name (e.g., "deepseek-chat")
    AGENT_MODEL_CIRCUIT_BREAKER_THRESHOLD: failures before opening (default: 5)
    AGENT_MODEL_CIRCUIT_BREAKER_COOLDOWN: seconds before half-open retry (default: 60)
"""

import logging
import time
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import BaseMessage

from app.config import settings

logger = logging.getLogger(__name__)


class CircuitBreaker:
    """Simple circuit breaker for a model provider.

    States:
        CLOSED  — normal operation, requests pass through
        OPEN    — too many failures, requests short-circuit immediately
        HALF_OPEN — cooldown expired, allow one probe request
    """

    def __init__(self, name: str, threshold: int = 5, cooldown: float = 60.0):
        self.name = name
        self.threshold = threshold
        self.cooldown = cooldown
        self._failures = 0
        self._last_failure_time: float = 0.0
        self._state = "closed"  # closed | open | half_open

    @property
    def is_open(self) -> bool:
        if self._state == "open":
            # Check if cooldown has elapsed → transition to half-open
            if time.monotonic() - self._last_failure_time > self.cooldown:
                self._state = "half_open"
                return False
            return True
        return False

    def record_success(self):
        self._failures = 0
        self._state = "closed"

    def record_failure(self):
        self._failures += 1
        self._last_failure_time = time.monotonic()
        if self._failures >= self.threshold:
            self._state = "open"
            logger.warning(
                "🔴 Circuit breaker OPEN for model '%s' after %d failures (cooldown %ds)",
                self.name, self._failures, self.cooldown,
            )


# Global circuit breakers per model name
_breakers: dict[str, CircuitBreaker] = {}

_CB_THRESHOLD = getattr(settings, "model_circuit_breaker_threshold", 5)
_CB_COOLDOWN = getattr(settings, "model_circuit_breaker_cooldown", 60.0)


def _get_breaker(model_name: str) -> CircuitBreaker:
    if model_name not in _breakers:
        _breakers[model_name] = CircuitBreaker(
            model_name, threshold=_CB_THRESHOLD, cooldown=_CB_COOLDOWN
        )
    return _breakers[model_name]


class ChatModelWithFallback(BaseChatModel):
    """Wraps a primary + fallback ChatModel with automatic failover.

    Delegates all calls to primary; on failure, retries on fallback.
    Implements bind_tools() passthrough so create_react_agent works.
    """

    primary: BaseChatModel
    fallback: BaseChatModel | None = None
    primary_name: str = "primary"
    fallback_name: str = "fallback"

    class Config:
        arbitrary_types_allowed = True

    @property
    def _llm_type(self) -> str:
        return "chat_model_with_fallback"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        """Sync generate with fallback."""
        breaker = _get_breaker(self.primary_name)

        if not breaker.is_open:
            try:
                result = self.primary._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
                breaker.record_success()
                return result
            except Exception as e:
                breaker.record_failure()
                logger.warning("⚠️ Primary model '%s' failed: %s. Trying fallback...", self.primary_name, e)
        else:
            logger.info("⏭️ Primary model '%s' circuit open, skipping to fallback", self.primary_name)

        if self.fallback:
            fb_breaker = _get_breaker(self.fallback_name)
            if not fb_breaker.is_open:
                try:
                    result = self.fallback._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
                    fb_breaker.record_success()
                    return result
                except Exception as e:
                    fb_breaker.record_failure()
                    logger.error("❌ Fallback model '%s' also failed: %s", self.fallback_name, e)
                    raise

        raise RuntimeError(
            f"All models unavailable (primary={self.primary_name}, fallback={self.fallback_name})"
        )

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        """Async generate with fallback."""
        breaker = _get_breaker(self.primary_name)

        if not breaker.is_open:
            try:
                result = await self.primary._agenerate(messages, stop=stop, run_manager=run_manager, **kwargs)
                breaker.record_success()
                return result
            except Exception as e:
                breaker.record_failure()
                logger.warning("⚠️ Primary model '%s' failed: %s. Trying fallback...", self.primary_name, e)
        else:
            logger.info("⏭️ Primary model '%s' circuit open, skipping to fallback", self.primary_name)

        if self.fallback:
            fb_breaker = _get_breaker(self.fallback_name)
            if not fb_breaker.is_open:
                try:
                    result = await self.fallback._agenerate(messages, stop=stop, run_manager=run_manager, **kwargs)
                    fb_breaker.record_success()
                    return result
                except Exception as e:
                    fb_breaker.record_failure()
                    logger.error("❌ Fallback model '%s' also failed: %s", self.fallback_name, e)
                    raise

        raise RuntimeError(
            f"All models unavailable (primary={self.primary_name}, fallback={self.fallback_name})"
        )

    def bind_tools(self, tools, **kwargs):
        """Bind tools to both primary and fallback models."""
        bound_primary = self.primary.bind_tools(tools, **kwargs)
        bound_fallback = self.fallback.bind_tools(tools, **kwargs) if self.fallback else None
        return ChatModelWithFallback(
            primary=bound_primary,
            fallback=bound_fallback,
            primary_name=self.primary_name,
            fallback_name=self.fallback_name,
        )
