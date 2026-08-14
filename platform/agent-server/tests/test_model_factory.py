from app.agent.model.fallback import ChatModelWithFallback, CircuitBreaker
from app.agent.model_fallback import (
    ChatModelWithFallback as LegacyChatModelWithFallback,
)
from app.agent.model_fallback import CircuitBreaker as LegacyCircuitBreaker


def test_legacy_fallback_module_reexports_model_owner():
    assert LegacyChatModelWithFallback is ChatModelWithFallback
    assert LegacyCircuitBreaker is CircuitBreaker


def test_circuit_breaker_opens_and_resets():
    breaker = CircuitBreaker("test", threshold=2, cooldown=60)

    breaker.record_failure()
    assert breaker.is_open is False
    breaker.record_failure()
    assert breaker.is_open is True
    breaker.record_success()
    assert breaker.is_open is False
