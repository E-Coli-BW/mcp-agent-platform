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
