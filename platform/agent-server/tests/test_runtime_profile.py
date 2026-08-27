from types import SimpleNamespace

from app.agent.execution_policy.runtime_profiles import (
    INVARIANT_POLICY,
    resolve_runtime_profile,
)


def test_profile_bounds_model_adaptive_autonomy():
    settings = SimpleNamespace(max_agent_steps=20, max_context_chars=20000)
    config = SimpleNamespace(
        id="customer-qa-agent",
        guardrails={"max_tool_calls": 8},
        runtime={"name": "local-7b", "max_tool_calls": 99, "allow_subagents": False},
    )
    profile = resolve_runtime_profile(config, settings)
    assert profile.max_tool_calls == 20
    assert profile.allow_subagents is False


def test_safety_invariants_are_not_profile_options():
    assert "approval_gate" in INVARIANT_POLICY
    assert "tenant_isolation" in INVARIANT_POLICY
    assert "no_cot" in INVARIANT_POLICY


def test_profile_parses_explicit_false_string_without_enabling_feature():
    settings = SimpleNamespace(
        max_agent_steps=20,
        max_context_chars=20000,
        direct_tool_routing_enabled=False,
    )
    config = SimpleNamespace(
        id="coding-agent",
        guardrails={},
        runtime={
            "allow_subagents": "false",
            "direct_tool_routing_enabled": "false",
        },
    )

    profile = resolve_runtime_profile(config, settings)

    assert profile.allow_subagents is False
    assert profile.direct_tool_routing_enabled is False


def test_configured_agent_removes_subagent_tool_when_profile_disallows_it(monkeypatch):
    from app.agent.agent_graph import factory

    captured = {}
    tools = [
        SimpleNamespace(name="file_read"),
        SimpleNamespace(name="spawn_subagent"),
    ]
    config = SimpleNamespace(
        id="coding-agent",
        knowledge={},
        model="qwen2.5:7b",
        tools=[tool.name for tool in tools],
        prompt="test prompt",
        guardrails={"max_tool_calls": 8},
        runtime={
            "allow_subagents": False,
            "max_context_chars": 60_000,
            "direct_tool_routing_enabled": True,
        },
    )

    monkeypatch.setattr(factory, "set_agent_contract", lambda *_args: None)
    monkeypatch.setattr(factory, "resolve_tools", lambda _names: tools)
    monkeypatch.setattr(factory, "create_chat_model", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(factory, "get_checkpointer", lambda: None)
    monkeypatch.setattr(
        factory,
        "build_agent_graph",
        lambda **kwargs: captured.update(kwargs) or "compiled-graph",
    )

    result = factory.create_agent_v2_from_config(config)

    assert result == "compiled-graph"
    assert [tool.name for tool in captured["tools"]] == ["file_read"]
    assert captured["max_steps"] == 8
    assert captured["max_context_chars"] == 60_000
    assert captured["direct_tool_routing_enabled"] is True
