"""Model fallback chain and per-provider circuit breakers."""

from __future__ import annotations

import logging
import time

from langchain_core.language_models.chat_models import BaseChatModel

from app.config import settings

logger = logging.getLogger(__name__)


class CircuitBreaker:
    """Open a provider circuit after repeated failures and retry after cooldown."""

    def __init__(
        self,
        name: str,
        threshold: int = 5,
        cooldown: float = 60.0,
    ) -> None:
        self.name = name
        self.threshold = threshold
        self.cooldown = cooldown
        self._failures = 0
        self._last_failure_time = 0.0
        self._state = "closed"

    @property
    def is_open(self) -> bool:
        if self._state != "open":
            return False
        if time.monotonic() - self._last_failure_time > self.cooldown:
            self._state = "half_open"
            return False
        return True

    def record_success(self) -> None:
        self._failures = 0
        self._state = "closed"

    def record_failure(self) -> None:
        self._failures += 1
        self._last_failure_time = time.monotonic()
        if self._failures >= self.threshold:
            self._state = "open"
            logger.warning(
                "Circuit breaker open for model %s after %d failures (cooldown %ds)",
                self.name,
                self._failures,
                self.cooldown,
            )


_breakers: dict[str, CircuitBreaker] = {}
_CB_THRESHOLD = getattr(settings, "model_circuit_breaker_threshold", 5)
_CB_COOLDOWN = getattr(settings, "model_circuit_breaker_cooldown", 60.0)


def _get_breaker(model_name: str) -> CircuitBreaker:
    if model_name not in _breakers:
        _breakers[model_name] = CircuitBreaker(
            model_name,
            threshold=_CB_THRESHOLD,
            cooldown=_CB_COOLDOWN,
        )
    return _breakers[model_name]


class ChatModelWithFallback(BaseChatModel):
    """Delegate to a fallback ChatModel when the primary provider fails."""

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
        breaker = _get_breaker(self.primary_name)
        if not breaker.is_open:
            try:
                result = self.primary._generate(
                    messages,
                    stop=stop,
                    run_manager=run_manager,
                    **kwargs,
                )
                breaker.record_success()
                return result
            except Exception as error:  # noqa: BLE001 - provider failover boundary
                breaker.record_failure()
                logger.warning(
                    "Primary model %s failed: %s; trying fallback",
                    self.primary_name,
                    error,
                )
        else:
            logger.info(
                "Primary model %s circuit open; using fallback",
                self.primary_name,
            )

        if self.fallback:
            fallback_breaker = _get_breaker(self.fallback_name)
            if not fallback_breaker.is_open:
                try:
                    result = self.fallback._generate(
                        messages,
                        stop=stop,
                        run_manager=run_manager,
                        **kwargs,
                    )
                    fallback_breaker.record_success()
                    return result
                except Exception:
                    fallback_breaker.record_failure()
                    raise

        raise RuntimeError(
            "All models unavailable "
            f"(primary={self.primary_name}, fallback={self.fallback_name})"
        )

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        breaker = _get_breaker(self.primary_name)
        if not breaker.is_open:
            try:
                result = await self.primary._agenerate(
                    messages,
                    stop=stop,
                    run_manager=run_manager,
                    **kwargs,
                )
                breaker.record_success()
                return result
            except Exception as error:  # noqa: BLE001 - provider failover boundary
                breaker.record_failure()
                logger.warning(
                    "Primary model %s failed: %s; trying fallback",
                    self.primary_name,
                    error,
                )
        else:
            logger.info(
                "Primary model %s circuit open; using fallback",
                self.primary_name,
            )

        if self.fallback:
            fallback_breaker = _get_breaker(self.fallback_name)
            if not fallback_breaker.is_open:
                try:
                    result = await self.fallback._agenerate(
                        messages,
                        stop=stop,
                        run_manager=run_manager,
                        **kwargs,
                    )
                    fallback_breaker.record_success()
                    return result
                except Exception:
                    fallback_breaker.record_failure()
                    raise

        raise RuntimeError(
            "All models unavailable "
            f"(primary={self.primary_name}, fallback={self.fallback_name})"
        )

    def bind_tools(self, tools, **kwargs):
        bound_primary = self.primary.bind_tools(tools, **kwargs)
        bound_fallback = (
            self.fallback.bind_tools(tools, **kwargs) if self.fallback else None
        )
        return ChatModelWithFallback(
            primary=bound_primary,
            fallback=bound_fallback,
            primary_name=self.primary_name,
            fallback_name=self.fallback_name,
        )
