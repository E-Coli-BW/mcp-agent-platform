"""Behavioral liveness contracts for public Agent configuration."""

from pathlib import Path

import pytest

from app.registry.config_loader import load_agent_config, load_all_configs


def test_unsupported_model_routing_fails_instead_of_being_ignored(tmp_path: Path):
    config_path = tmp_path / "agent.yaml"
    config_path.write_text(
        """
id: demo
name: Demo
model: qwen2.5:7b
routing:
  complex_model: openai/gpt-4o
  simple_model: qwen2.5:7b
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="routing"):
        load_agent_config(str(config_path))


def test_production_config_directory_fails_on_unsupported_agent(tmp_path: Path):
    (tmp_path / "invalid.yaml").write_text(
        """
id: invalid
name: Invalid Agent
routing:
  strategy: complexity
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="routing"):
        load_all_configs(str(tmp_path))


def test_shipped_agent_configs_do_not_use_unsupported_routing():
    config_dir = Path(__file__).parents[1] / "agents"

    configs = load_all_configs(str(config_dir))

    assert set(configs) == {"coding-agent", "customer-qa-agent", "ops-agent"}
    assert all(not config.routing for config in configs.values())
