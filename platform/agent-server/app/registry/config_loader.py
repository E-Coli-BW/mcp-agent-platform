"""Load agent configs from YAML files."""

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class AgentConfig:
    """Parsed agent configuration from YAML."""

    id: str
    name: str
    description: str = ""
    version: str = "1.0"
    model: str = "qwen2.5:7b"
    prompt: str = ""
    tools: list[str] = field(default_factory=list)
    guardrails: dict = field(default_factory=dict)
    routing: dict = field(default_factory=dict)
    knowledge: dict = field(default_factory=dict)
    runtime: dict = field(default_factory=dict)


def load_agent_config(path: str) -> AgentConfig:
    """Load a single agent config from a YAML file."""
    import yaml

    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    routing = raw.get("routing") or {}
    if routing:
        raise ValueError(
            "Agent routing is not supported; configure one explicit model per agent"
        )
    return AgentConfig(
        id=raw["id"],
        name=raw["name"],
        description=raw.get("description", ""),
        version=raw.get("version", "1.0"),
        model=raw.get("model", "qwen2.5:7b"),
        prompt=raw.get("prompt", ""),
        tools=raw.get("tools", []),
        guardrails=raw.get("guardrails", {}),
        routing={},
        knowledge=raw.get("knowledge", {}),
        runtime=raw.get("runtime", {}),
    )


def load_all_configs(config_dir: str) -> dict[str, AgentConfig]:
    """Load all agent configs from a directory.

    Returns a dict mapping agent ID to AgentConfig.
    Invalid files fail the whole load so declared agents cannot disappear silently.
    """
    configs: dict[str, AgentConfig] = {}
    config_path = Path(config_dir)
    if not config_path.exists():
        return configs
    for config_file in sorted(config_path.glob("*.yaml")):
        config = load_agent_config(str(config_file))
        configs[config.id] = config
    return configs
