from types import SimpleNamespace

from app.agent.runtime_profile import resolve_runtime_profile


def test_profiles_have_bounded_autonomy_matrix():
    settings = SimpleNamespace(max_agent_steps=20, max_context_chars=20000)
    configs = [
        SimpleNamespace(id="customer-qa-agent", guardrails={}, runtime={"max_tool_calls": 4, "allow_subagents": False}),
        SimpleNamespace(id="coding-agent", guardrails={}, runtime={"max_tool_calls": 12, "allow_subagents": True}),
    ]
    profiles = [resolve_runtime_profile(config, settings) for config in configs]
    assert [profile.max_tool_calls for profile in profiles] == [4, 12]
    assert profiles[0].allow_subagents is False
    assert profiles[1].allow_subagents is True
