from __future__ import annotations

from types import SimpleNamespace

import pytest


@pytest.fixture(autouse=True)
def reset_agent_registry():
    from app.agent.engine.registry import get_agent_registry

    registry = get_agent_registry()
    registry.invalidate()
    yield
    registry.invalidate()


class TestPromptGovernedAgentCache:
    @pytest.mark.asyncio
    async def test_runtime_profile_change_rebuilds_configured_graph(self, monkeypatch):
        import app.agent.agent_graph.factory as graph_factory
        import app.registry.config_loader as config_loader_mod
        from app.agent.engine.registry import get_agent_registry

        config = SimpleNamespace(
            id="coding-agent",
            model="qwen2.5:7b",
            prompt="prompt",
            tools=["file_read", "spawn_subagent"],
            guardrails={},
            routing={},
            knowledge={},
            runtime={"allow_subagents": False},
        )
        built = []

        def fake_create_agent(active_config, temperature=None):
            agent = object()
            built.append(agent)
            return agent

        monkeypatch.setattr(
            graph_factory,
            "create_agent_v2_from_config",
            fake_create_agent,
        )
        monkeypatch.setattr(
            config_loader_mod,
            "load_all_configs",
            lambda _directory: {"coding-agent": config},
        )

        registry = get_agent_registry()
        first = await registry.get_agent("coding-agent", temperature=0.0)
        config.runtime = {"allow_subagents": True}
        second = await registry.get_agent("coding-agent", temperature=0.0)

        assert first is not second
        assert len(built) == 2

    @pytest.mark.asyncio
    async def test_removing_config_rebuilds_as_fallback_graph(self, monkeypatch):
        import app.agent.agent_graph.factory as graph_factory
        import app.registry.config_loader as config_loader_mod
        import app.tools.definitions as tool_definitions
        from app.agent.engine.registry import get_agent_registry

        config = SimpleNamespace(
            id="coding-agent",
            model="qwen2.5:7b",
            prompt="prompt",
            tools=["file_read"],
            guardrails={},
            routing={},
            knowledge={},
            runtime={},
        )
        configs = {"coding-agent": config}
        configured = object()
        fallback = object()
        tool = SimpleNamespace(name="file_read")

        monkeypatch.setattr(
            graph_factory,
            "create_agent_v2_from_config",
            lambda *_args, **_kwargs: configured,
        )
        monkeypatch.setattr(
            graph_factory,
            "create_agent_v2",
            lambda *_args, **_kwargs: fallback,
        )
        monkeypatch.setattr(
            config_loader_mod,
            "load_all_configs",
            lambda _directory: configs,
        )
        monkeypatch.setattr(tool_definitions, "get_tools", lambda: [tool])

        registry = get_agent_registry()
        first = await registry.get_agent("coding-agent", temperature=0.0)
        configs.clear()
        second = await registry.get_agent("coding-agent", temperature=0.0)

        assert first is configured
        assert second is fallback

    @pytest.mark.asyncio
    async def test_prompt_version_isolation_in_cache(self, monkeypatch):
        import app.agent.agent_graph.factory as graph_factory
        import app.registry.config_loader as config_loader_mod
        from app.agent.engine.registry import get_agent_registry
        from app.config import settings

        monkeypatch.setattr(settings, "prompt_allow_request_override", True)
        monkeypatch.setattr(settings, "prompt_tenant_versions_json", "{}")
        monkeypatch.setattr(settings, "prompt_canary_enabled", False)

        seen = []

        def fake_create_agent(model_name=None, temperature=None):
            obj = object()
            seen.append((temperature, obj))
            return obj

        monkeypatch.setattr(graph_factory, "create_agent_v2", fake_create_agent)
        monkeypatch.setattr(config_loader_mod, "load_all_configs", lambda _d: {})

        registry = get_agent_registry()
        a_v1 = await registry.get_agent(
            "qwen2.5:7b",
            temperature=0.0,
            tenant_id="tenant-a",
            session_id="tenant-a:s1",
            prompt_version="v1",
        )
        a_v2 = await registry.get_agent(
            "qwen2.5:7b",
            temperature=0.0,
            tenant_id="tenant-a",
            session_id="tenant-a:s1",
            prompt_version="v2",
        )

        assert a_v1 is not a_v2

    @pytest.mark.asyncio
    async def test_tenant_isolation_in_cache(self, monkeypatch):
        import app.agent.agent_graph.factory as graph_factory
        import app.registry.config_loader as config_loader_mod
        from app.agent.engine.registry import get_agent_registry
        from app.config import settings

        monkeypatch.setattr(settings, "prompt_allow_request_override", False)
        monkeypatch.setattr(settings, "prompt_tenant_versions_json", "{}")
        monkeypatch.setattr(settings, "prompt_canary_enabled", False)

        call_count = {"n": 0}

        def fake_create_agent(model_name=None, temperature=None):
            call_count["n"] += 1
            return object()

        monkeypatch.setattr(graph_factory, "create_agent_v2", fake_create_agent)
        monkeypatch.setattr(config_loader_mod, "load_all_configs", lambda _d: {})

        registry = get_agent_registry()
        a_t1 = await registry.get_agent(
            "qwen2.5:7b",
            temperature=0.0,
            tenant_id="tenant-a",
            session_id="tenant-a:s1",
        )
        a_t2 = await registry.get_agent(
            "qwen2.5:7b",
            temperature=0.0,
            tenant_id="tenant-b",
            session_id="tenant-b:s1",
        )

        assert a_t1 is not a_t2
        assert call_count["n"] == 2

