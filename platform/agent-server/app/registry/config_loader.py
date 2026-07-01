"""Load agent configs from YAML files."""

import logging
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class AgentConfig:
    """Parsed agent configuration from YAML."""

    id: str
    name: str
    version: str = "1.0"
    model: str = "qwen2.5:7b"
    prompt: str = ""
    tools: list[str] = field(default_factory=list)
    guardrails: dict = field(default_factory=dict)
    routing: dict = field(default_factory=dict)


def load_agent_config(path: str) -> AgentConfig:
    """Load a single agent config from a YAML file."""
    import yaml

    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    return AgentConfig(
        id=raw["id"],
        name=raw["name"],
        version=raw.get("version", "1.0"),
        model=raw.get("model", "qwen2.5:7b"),
        prompt=raw.get("prompt", ""),
        tools=raw.get("tools", []),
        guardrails=raw.get("guardrails", {}),
        routing=raw.get("routing", {}),
    )


def load_all_configs(config_dir: str) -> dict[str, AgentConfig]:
    """Load all agent configs from a directory.

    Returns a dict mapping agent ID to AgentConfig.
    Skips files that fail to parse with a warning.
    """
    configs: dict[str, AgentConfig] = {}
    config_path = Path(config_dir)
    if not config_path.exists():
        return configs
    for f in config_path.glob("*.yaml"):
        try:
            cfg = load_agent_config(str(f))
            configs[cfg.id] = cfg
        except Exception as e:
            logger.warning("Failed to load %s: %s", f, e)
    return configs
