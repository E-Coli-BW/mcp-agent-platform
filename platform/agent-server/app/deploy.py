"""Deployment profile loader — supports local/cloud/hybrid via config file.

Resolves ${ENV_VAR:default} placeholders in YAML values.
Usage:
    profile = load_deployment_profile()  # reads AGENT_DEPLOY_PROFILE env var
    profile.apply(settings)              # overrides settings in-place
"""

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

_ENV_PATTERN = re.compile(r"\$\{(\w+)(?::([^}]*))?\}")


def _resolve_env(value: str) -> str:
    """Replace ${VAR:default} placeholders with environment variable values."""
    if not isinstance(value, str):
        return value

    def _replace(match: re.Match) -> str:
        var_name = match.group(1)
        default = match.group(2) or ""
        return os.environ.get(var_name, default)

    return _ENV_PATTERN.sub(_replace, value)


@dataclass
class DeploymentProfile:
    """Resolved deployment profile."""

    name: str
    services: dict[str, str] = field(default_factory=dict)
    options: dict[str, object] = field(default_factory=dict)

    def apply(self, settings_obj) -> None:
        """Override settings attributes with profile values."""
        for key, value in self.services.items():
            if hasattr(settings_obj, key):
                resolved = _resolve_env(value)
                # pydantic-settings: use model's __dict__ directly for mutable override
                settings_obj.__dict__[key] = resolved
                logger.info("Profile [%s] override: %s", self.name, key)
        logger.info("✅ Applied deployment profile: %s", self.name)


def load_deployment_profile(
    config_path: str | None = None,
    profile_name: str | None = None,
) -> DeploymentProfile:
    """Load a deployment profile from YAML config.

    Args:
        config_path: Path to deployment.yaml. Default: config/deployment.yaml
        profile_name: Profile to load. Default: AGENT_DEPLOY_PROFILE env var or 'local'
    """
    if config_path is None:
        # Look relative to project root (agent-server/)
        base = Path(__file__).parent.parent
        config_path = str(base / "config" / "deployment.yaml")

    if not Path(config_path).exists():
        logger.warning("Deployment config not found: %s — using defaults", config_path)
        return DeploymentProfile(name="default")

    profile_name = profile_name or os.environ.get("AGENT_DEPLOY_PROFILE", "local")

    with open(config_path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    profiles = raw.get("profiles", {})
    if profile_name not in profiles:
        logger.warning(
            "Profile '%s' not found in %s — available: %s",
            profile_name,
            config_path,
            list(profiles.keys()),
        )
        return DeploymentProfile(name=profile_name)

    profile_data = profiles[profile_name]
    services = {k: _resolve_env(v) for k, v in profile_data.get("services", {}).items()}
    options = profile_data.get("options", {})

    return DeploymentProfile(name=profile_name, services=services, options=options)
