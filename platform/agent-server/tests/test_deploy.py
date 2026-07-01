"""Tests for deployment profile loader."""

import os
import tempfile

import pytest

from app.deploy import DeploymentProfile, load_deployment_profile, _resolve_env


# ── _resolve_env ──────────────────────────────────────────────


def test_resolve_env_with_set_var(monkeypatch):
    monkeypatch.setenv("MY_TEST_URL", "https://prod.example.com")
    assert _resolve_env("${MY_TEST_URL:http://localhost}") == "https://prod.example.com"


def test_resolve_env_with_default():
    # Ensure var is not set
    os.environ.pop("NONEXISTENT_VAR_XYZ", None)
    assert _resolve_env("${NONEXISTENT_VAR_XYZ:http://fallback}") == "http://fallback"


def test_resolve_env_no_placeholder():
    assert _resolve_env("http://localhost:8180") == "http://localhost:8180"


def test_resolve_env_multiple_placeholders(monkeypatch):
    monkeypatch.setenv("HOST", "prod.com")
    result = _resolve_env("https://${HOST:localhost}:${PORT:8080}")
    assert result == "https://prod.com:8080"


# ── load_deployment_profile ───────────────────────────────────


def test_load_local_profile(tmp_path):
    config = tmp_path / "deployment.yaml"
    config.write_text("""
profiles:
  local:
    services:
      memory_server_url: "http://localhost:8180"
      redis_url: "redis://localhost:6379/0"
    options:
      graceful_degradation: true
""")
    profile = load_deployment_profile(str(config), "local")
    assert profile.name == "local"
    assert profile.services["memory_server_url"] == "http://localhost:8180"
    assert profile.options["graceful_degradation"] is True


def test_load_cloud_profile_with_env_resolution(tmp_path, monkeypatch):
    monkeypatch.setenv("CLOUD_MEMORY_URL", "https://memory.cloud.io")
    config = tmp_path / "deployment.yaml"
    config.write_text("""
profiles:
  cloud:
    services:
      memory_server_url: "${CLOUD_MEMORY_URL:https://default.cloud.io}"
    options:
      graceful_degradation: false
""")
    profile = load_deployment_profile(str(config), "cloud")
    assert profile.services["memory_server_url"] == "https://memory.cloud.io"


def test_load_missing_profile(tmp_path):
    config = tmp_path / "deployment.yaml"
    config.write_text("profiles:\n  local:\n    services: {}\n")
    profile = load_deployment_profile(str(config), "nonexistent")
    assert profile.name == "nonexistent"
    assert profile.services == {}


def test_load_missing_file():
    profile = load_deployment_profile("/nonexistent/path.yaml")
    assert profile.name == "default"


def test_profile_apply():
    """Test that apply() overrides settings attributes."""

    class FakeSettings:
        memory_server_url = "http://old:8180"
        redis_url = "redis://old:6379"
        nonexistent_field = "keep"

    settings = FakeSettings()
    profile = DeploymentProfile(
        name="test",
        services={
            "memory_server_url": "http://new:8180",
            "redis_url": "redis://new:6379",
        },
    )
    profile.apply(settings)
    assert settings.memory_server_url == "http://new:8180"
    assert settings.redis_url == "redis://new:6379"


def test_env_profile_selection(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_DEPLOY_PROFILE", "cloud")
    config = tmp_path / "deployment.yaml"
    config.write_text("""
profiles:
  local:
    services:
      memory_server_url: "http://localhost:8180"
  cloud:
    services:
      memory_server_url: "https://cloud:8180"
""")
    profile = load_deployment_profile(str(config))
    assert profile.name == "cloud"
    assert profile.services["memory_server_url"] == "https://cloud:8180"
