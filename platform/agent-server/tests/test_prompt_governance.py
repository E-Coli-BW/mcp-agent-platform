from __future__ import annotations

from app.agent.prompts import resolve_system_prompt
from app.context.request_context import set_prompt_version
from app.config import settings


def test_default_prompt_resolution(monkeypatch):
    monkeypatch.setattr(settings, "prompt_version", "v2")
    monkeypatch.setattr(settings, "prompt_allow_request_override", False)
    monkeypatch.setattr(settings, "prompt_tenant_versions_json", "{}")
    monkeypatch.setattr(settings, "prompt_canary_enabled", False)

    r = resolve_system_prompt(tenant_id="t1", session_id="s1")
    assert r.version == "v2"
    assert r.assignment_source == "default"
    assert r.content_hash.startswith("sha256:")


def test_request_override_enabled(monkeypatch):
    monkeypatch.setattr(settings, "prompt_version", "v2")
    monkeypatch.setattr(settings, "prompt_allow_request_override", True)
    monkeypatch.setattr(settings, "prompt_tenant_versions_json", "{}")
    monkeypatch.setattr(settings, "prompt_canary_enabled", False)

    r = resolve_system_prompt(
        requested_version="v1",
        tenant_id="t1",
        session_id="s1",
    )
    assert r.version == "v1"
    assert r.assignment_source == "request_override"


def test_request_override_disabled(monkeypatch):
    monkeypatch.setattr(settings, "prompt_version", "v2")
    monkeypatch.setattr(settings, "prompt_allow_request_override", False)
    monkeypatch.setattr(settings, "prompt_tenant_versions_json", "{}")
    monkeypatch.setattr(settings, "prompt_canary_enabled", False)

    r = resolve_system_prompt(
        requested_version="v1",
        tenant_id="t1",
        session_id="s1",
    )
    assert r.version == "v2"
    assert r.assignment_source == "default"


def test_tenant_override(monkeypatch):
    monkeypatch.setattr(settings, "prompt_version", "v2")
    monkeypatch.setattr(settings, "prompt_allow_request_override", False)
    monkeypatch.setattr(settings, "prompt_tenant_versions_json", '{"tenant-a": "v1"}')
    monkeypatch.setattr(settings, "prompt_canary_enabled", False)

    r = resolve_system_prompt(tenant_id="tenant-a", session_id="s1")
    assert r.version == "v1"
    assert r.assignment_source == "tenant_override"


def test_canary_routing_100_percent(monkeypatch):
    monkeypatch.setattr(settings, "prompt_version", "v2")
    monkeypatch.setattr(settings, "prompt_allow_request_override", False)
    monkeypatch.setattr(settings, "prompt_tenant_versions_json", "{}")
    monkeypatch.setattr(settings, "prompt_canary_enabled", True)
    monkeypatch.setattr(settings, "prompt_canary_percent", 100)
    monkeypatch.setattr(settings, "prompt_canary_version", "v1")
    monkeypatch.setattr(settings, "prompt_canary_tenants", "")

    r = resolve_system_prompt(tenant_id="t1", session_id="s1")
    assert r.version == "v1"
    assert r.assignment_source == "canary"


def test_canary_tenant_allowlist(monkeypatch):
    monkeypatch.setattr(settings, "prompt_version", "v2")
    monkeypatch.setattr(settings, "prompt_allow_request_override", False)
    monkeypatch.setattr(settings, "prompt_tenant_versions_json", "{}")
    monkeypatch.setattr(settings, "prompt_canary_enabled", True)
    monkeypatch.setattr(settings, "prompt_canary_percent", 100)
    monkeypatch.setattr(settings, "prompt_canary_version", "v1")
    monkeypatch.setattr(settings, "prompt_canary_tenants", "tenant-a,tenant-b")

    allowed = resolve_system_prompt(tenant_id="tenant-a", session_id="s1")
    blocked = resolve_system_prompt(tenant_id="tenant-z", session_id="s1")

    assert allowed.version == "v1"
    assert allowed.assignment_source == "canary"
    assert blocked.version == "v2"
    assert blocked.assignment_source == "default"


def test_context_prompt_version_override(monkeypatch):
    monkeypatch.setattr(settings, "prompt_version", "v2")
    monkeypatch.setattr(settings, "prompt_allow_request_override", True)
    monkeypatch.setattr(settings, "prompt_tenant_versions_json", "{}")
    monkeypatch.setattr(settings, "prompt_canary_enabled", False)

    set_prompt_version("v1")
    r = resolve_system_prompt(
        requested_version="v1",
        tenant_id="t1",
        session_id="s1",
    )
    assert r.version == "v1"
    assert r.assignment_source == "request_override"

