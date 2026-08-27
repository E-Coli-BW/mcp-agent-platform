"""Contracts for the configured coding agent's repository exploration policy."""

from pathlib import Path

from app.registry.config_loader import load_agent_config


def test_coding_agent_uses_search_first_for_conceptual_questions():
    config_path = Path(__file__).parents[1] / "agents" / "coding-agent.yaml"

    config = load_agent_config(str(config_path))
    prompt = config.prompt

    assert "DEFAULT first tool for conceptual project questions" in prompt
    assert "A zero-result natural-language search is inconclusive" in prompt
    assert "Use search-result line numbers to read" in prompt
    assert "A snippet containing only `StartAt` is insufficient" in prompt
    assert "Never infer an additional parallel branch from a `Catch` transition" in prompt
    assert "Do not collapse adjacent states" in prompt
    assert "Never end with a promised future action" in prompt
    assert "The workspace is NOT mounted" in prompt
    assert "ALWAYS start here when exploring a new project" not in prompt
    assert config.runtime == {
        "name": "local-qwen-7b",
        "max_tool_calls": 12,
        "max_context_chars": 10500,
        "verification_enabled": False,
        "allow_subagents": False,
        "direct_tool_routing_enabled": True,
    }
