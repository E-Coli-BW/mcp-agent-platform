from app.agent.model.fallback import CircuitBreaker


def test_circuit_breaker_opens_and_resets():
    breaker = CircuitBreaker("test", threshold=2, cooldown=60)

    breaker.record_failure()
    assert breaker.is_open is False
    breaker.record_failure()
    assert breaker.is_open is True
    breaker.record_success()
    assert breaker.is_open is False
