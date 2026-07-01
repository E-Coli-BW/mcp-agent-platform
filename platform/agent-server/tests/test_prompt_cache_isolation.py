from __future__ import annotations

import pytest


class TestPromptGovernedAgentCache:
    @pytest.mark.asyncio
    async def test_prompt_version_isolation_in_cache(self, monkeypatch):
        import app.agent.graph as graph_mod
        import app.registry.config_loader as config_loader_mod
        from app.config import settings

        monkeypatch.setattr(settings, "prompt_allow_request_override", True)
        monkeypatch.setattr(settings, "prompt_tenant_versions_json", "{}")
        monkeypatch.setattr(settings, "prompt_canary_enabled", False)

        seen = []

        def fake_create_agent(model_name=None, temperature=None):
            obj = object()
            seen.append((temperature, obj))
            return obj

        monkeypatch.setattr(graph_mod, "create_agent", fake_create_agent)
        monkeypatch.setattr(config_loader_mod, "load_all_configs", lambda _d: {})
        monkeypatch.setattr(graph_mod, "_agents", {})
        monkeypatch.setattr(graph_mod, "_agent_tool_names", {})

        a_v1 = await graph_mod.get_agent(
            "qwen2.5:7b",
            temperature=0.0,
            tenant_id="tenant-a",
            session_id="tenant-a:s1",
            prompt_version="v1",
        )
        a_v2 = await graph_mod.get_agent(
            "qwen2.5:7b",
            temperature=0.0,
            tenant_id="tenant-a",
            session_id="tenant-a:s1",
            prompt_version="v2",
        )

        assert a_v1 is not a_v2

    @pytest.mark.asyncio
    async def test_tenant_isolation_in_cache(self, monkeypatch):
        import app.agent.graph as graph_mod
        import app.registry.config_loader as config_loader_mod
        from app.config import settings

        # Force v1 path: this test only mocks create_agent (v1), but
        # get_agent dispatches to create_agent_v2 when graph_version=v2
        # (the default). See test_temperature_plumbing.py for the same fix.
        monkeypatch.setattr(settings, "agent_graph_version", "v1")
        monkeypatch.setattr(settings, "prompt_allow_request_override", False)
        monkeypatch.setattr(settings, "prompt_tenant_versions_json", "{}")
        monkeypatch.setattr(settings, "prompt_canary_enabled", False)

        call_count = {"n": 0}

        def fake_create_agent(model_name=None, temperature=None):
            call_count["n"] += 1
            return object()

        monkeypatch.setattr(graph_mod, "create_agent", fake_create_agent)
        monkeypatch.setattr(config_loader_mod, "load_all_configs", lambda _d: {})
        monkeypatch.setattr(graph_mod, "_agents", {})
        monkeypatch.setattr(graph_mod, "_agent_tool_names", {})

        a_t1 = await graph_mod.get_agent(
            "qwen2.5:7b",
            temperature=0.0,
            tenant_id="tenant-a",
            session_id="tenant-a:s1",
        )
        a_t2 = await graph_mod.get_agent(
            "qwen2.5:7b",
            temperature=0.0,
            tenant_id="tenant-b",
            session_id="tenant-b:s1",
        )

        assert a_t1 is not a_t2
        assert call_count["n"] == 2

