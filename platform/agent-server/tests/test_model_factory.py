from app.agent.model.factory import create_chat_model
from app.agent.model.fallback import CircuitBreaker
from app.config import settings


def test_circuit_breaker_opens_and_resets():
    breaker = CircuitBreaker("test", threshold=2, cooldown=60)

    breaker.record_failure()
    assert breaker.is_open is False
    breaker.record_failure()
    assert breaker.is_open is True
    breaker.record_success()
    assert breaker.is_open is False


def test_mlx_model_can_disable_thinking_without_model_name_hardcoding(monkeypatch):
    monkeypatch.setattr(settings, "mlx_enable_thinking", False)

    model = create_chat_model("mlx/Qwen3.6-27B-MLX-4bit", temperature=0.6)

    assert model.extra_body == {"enable_thinking": False}


def test_mlx_model_omits_thinking_override_by_default(monkeypatch):
    monkeypatch.setattr(settings, "mlx_enable_thinking", None)

    model = create_chat_model("mlx/Qwen2.5-0.5B-Instruct-4bit")

    assert model.extra_body is None


def test_ollama_model_enforces_generation_limit(monkeypatch):
    captured = {}

    def fake_ollama(**kwargs):
        captured.update(kwargs)
        return object()

    import langchain_ollama

    monkeypatch.setattr(langchain_ollama, "ChatOllama", fake_ollama)
    monkeypatch.setattr(settings, "max_tokens", 1024)

    create_chat_model("qwen2.5:7b")

    assert captured["num_predict"] == 1024


def test_ollama_model_honors_lower_agent_limit(monkeypatch):
    captured = {}

    def fake_ollama(**kwargs):
        captured.update(kwargs)
        return object()

    import langchain_ollama

    monkeypatch.setattr(langchain_ollama, "ChatOllama", fake_ollama)
    monkeypatch.setattr(settings, "max_tokens", 4096)

    create_chat_model("qwen2.5:7b", max_tokens=1024)

    assert captured["num_predict"] == 1024
