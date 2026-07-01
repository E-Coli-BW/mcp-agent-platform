"""Tests for request → graph temperature plumbing.

Until this fix, `chat.py` accepted `temperature` in the OpenAI-compatible
request schema but silently dropped it; `graph.py::_create_chat_model`
hardcoded T=0.7 in all three provider branches (Ollama / OpenAI /
Anthropic). The eval harness sent `temperature: 0` expecting
deterministic decoding and got T=0.7 instead — directly responsible
for the v5 case being flaky 3/5 instead of converging.

These tests pin down the wiring at the unit-test layer (no real LLM,
no real network) so a future refactor can't silently re-introduce the
bug. Three things must hold:

  1. `_create_chat_model(temperature=X)` forwards X to the provider class.
  2. `_create_chat_model()` (no override) honours `settings.default_temperature`.
  3. `get_agent` cache key includes temperature — two requests at
     different T must NOT share a cached agent instance.
"""
from __future__ import annotations

import pytest


# ── Layer 1: _create_chat_model plumbs `temperature` ─────────────────


class TestCreateChatModelTemperature:
    def test_default_uses_settings(self, monkeypatch):
        """No `temperature` arg ⇒ value of settings.default_temperature."""
        import app.agent.graph as graph_mod
        from app.config import settings

        # Force a known default to be sure we're not seeing a coincidence.
        monkeypatch.setattr(settings, "default_temperature", 0.42)

        captured: dict = {}

        def fake_ollama(**kwargs):
            captured.update(kwargs)
            return object()

        # Patch ChatOllama right where the lazy import happens — inside
        # the Ollama branch of _create_chat_model.
        import langchain_ollama

        monkeypatch.setattr(langchain_ollama, "ChatOllama", fake_ollama)

        graph_mod._create_chat_model("qwen2.5:7b")
        assert captured["temperature"] == 0.42

    def test_explicit_override_wins(self, monkeypatch):
        """Explicit `temperature=X` wins over settings.default_temperature."""
        import app.agent.graph as graph_mod
        from app.config import settings

        monkeypatch.setattr(settings, "default_temperature", 0.7)

        captured: dict = {}

        def fake_ollama(**kwargs):
            captured.update(kwargs)
            return object()

        import langchain_ollama

        monkeypatch.setattr(langchain_ollama, "ChatOllama", fake_ollama)

        graph_mod._create_chat_model("qwen2.5:7b", temperature=0.0)
        # Critical: 0.0 is falsy. The "or settings.default_temperature"
        # anti-pattern would swallow it back to 0.7. This test catches
        # that regression.
        assert captured["temperature"] == 0.0

    def test_none_temperature_falls_back_to_default(self, monkeypatch):
        """`temperature=None` is equivalent to omitting the arg."""
        import app.agent.graph as graph_mod
        from app.config import settings

        monkeypatch.setattr(settings, "default_temperature", 0.55)

        captured: dict = {}

        def fake_ollama(**kwargs):
            captured.update(kwargs)
            return object()

        import langchain_ollama

        monkeypatch.setattr(langchain_ollama, "ChatOllama", fake_ollama)

        graph_mod._create_chat_model("qwen2.5:7b", temperature=None)
        assert captured["temperature"] == 0.55


# ── Layer 2: get_agent cache key includes temperature ────────────────


class TestGetAgentCacheKey:
    @pytest.mark.asyncio
    async def test_different_temperatures_get_different_instances(
        self, monkeypatch
    ):
        """Same model + different temperature MUST yield distinct agents.

        ChatOllama bakes `temperature` into the model instance; if the
        cache key ignored temperature, the first request's T value would
        leak into every subsequent request regardless of their T.
        """
        import app.agent.graph as graph_mod
        import app.registry.config_loader as config_loader_mod
        from app.config import settings

        # Force v1 path — these tests only mock `create_agent` (v1).
        # When agent_graph_version="v2" (the default), get_agent dispatches
        # to `create_agent_v2` which bypasses our mock and the assertions
        # under-count construction calls. The cache-key logic being tested
        # is identical for both paths, so v1 is the cheaper surface.
        monkeypatch.setattr(settings, "agent_graph_version", "v1")

        # Sentinel objects to identify instances unambiguously.
        sentinel_t0 = object()
        sentinel_t07 = object()
        seen_temps: list[float | None] = []

        def fake_create_agent(model_name=None, temperature=None):
            seen_temps.append(temperature)
            # Return a different sentinel per temperature so we can verify
            # the cache returns the right one on the second call.
            return sentinel_t0 if temperature == 0.0 else sentinel_t07

        monkeypatch.setattr(graph_mod, "create_agent", fake_create_agent)
        # Force the "no YAML config matches" branch — go through
        # create_agent rather than create_agent_from_config. `load_all_configs`
        # is imported lazily inside get_agent, so patch its source module.
        monkeypatch.setattr(config_loader_mod, "load_all_configs", lambda _d: {})
        # Reset the module-level cache between tests so prior runs don't leak.
        monkeypatch.setattr(graph_mod, "_agents", {})
        monkeypatch.setattr(graph_mod, "_agent_tool_names", {})

        a0 = await graph_mod.get_agent("qwen2.5:7b", temperature=0.0)
        a07 = await graph_mod.get_agent("qwen2.5:7b", temperature=0.7)
        assert a0 is sentinel_t0
        assert a07 is sentinel_t07
        # Two distinct construction calls — proves the cache treated
        # them as different keys.
        assert 0.0 in seen_temps
        assert 0.7 in seen_temps

    @pytest.mark.asyncio
    async def test_same_temperature_hits_cache(self, monkeypatch):
        """Identical (model, temperature) MUST reuse the cached instance.

        Otherwise we'd burn one ChatOllama construction per request,
        breaking the agent-cache contract everyone else relies on.
        """
        import app.agent.graph as graph_mod
        import app.registry.config_loader as config_loader_mod
        from app.config import settings

        monkeypatch.setattr(settings, "agent_graph_version", "v1")

        call_count = {"n": 0}

        def fake_create_agent(model_name=None, temperature=None):
            call_count["n"] += 1
            return object()

        monkeypatch.setattr(graph_mod, "create_agent", fake_create_agent)
        monkeypatch.setattr(config_loader_mod, "load_all_configs", lambda _d: {})
        monkeypatch.setattr(graph_mod, "_agents", {})
        monkeypatch.setattr(graph_mod, "_agent_tool_names", {})

        a1 = await graph_mod.get_agent("qwen2.5:7b", temperature=0.0)
        a2 = await graph_mod.get_agent("qwen2.5:7b", temperature=0.0)
        assert a1 is a2
        assert call_count["n"] == 1  # only one construction

    @pytest.mark.asyncio
    async def test_default_and_none_share_cache(self, monkeypatch):
        """`temperature=None` and explicit settings.default_temperature
        MUST resolve to the same cache entry. Otherwise a chat client that
        omits temperature and one that explicitly passes the default would
        end up with two separate model instances for no reason."""
        import app.agent.graph as graph_mod
        import app.registry.config_loader as config_loader_mod
        from app.config import settings

        monkeypatch.setattr(settings, "agent_graph_version", "v1")
        monkeypatch.setattr(settings, "default_temperature", 0.7)

        call_count = {"n": 0}

        def fake_create_agent(model_name=None, temperature=None):
            call_count["n"] += 1
            return object()

        monkeypatch.setattr(graph_mod, "create_agent", fake_create_agent)
        monkeypatch.setattr(config_loader_mod, "load_all_configs", lambda _d: {})
        monkeypatch.setattr(graph_mod, "_agents", {})
        monkeypatch.setattr(graph_mod, "_agent_tool_names", {})

        a1 = await graph_mod.get_agent("qwen2.5:7b", temperature=None)
        a2 = await graph_mod.get_agent("qwen2.5:7b", temperature=0.7)
        assert a1 is a2
        assert call_count["n"] == 1
