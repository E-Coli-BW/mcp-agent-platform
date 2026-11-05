"""Production-composition contracts for Agent SDK plugins."""

from __future__ import annotations

import json
import sys
import textwrap
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.auth.middleware import (
    AuthContext,
    reset_active_auth_context,
    set_active_auth_context,
    tenant_context,
)
from app.context.request_context import set_agent_contract, set_request_context
from app.plugins import loader
from app.registry import tool_registry


def _write_plugin(
    root: Path,
    *,
    body: str,
    permissions: tuple[str, ...] = (),
    secrets: tuple[str, ...] = (),
    language: str = "python",
    extra_tool_yaml: str = "",
    extra_plugin_yaml: str = "",
) -> Path:
    module_name = f"plugin_{uuid.uuid4().hex}"
    plugin_dir = root / module_name
    plugin_dir.mkdir()
    (plugin_dir / f"{module_name}.py").write_text(
        textwrap.dedent(body),
        encoding="utf-8",
    )
    permission_yaml = "\n".join(f"        - {item}" for item in permissions)
    secret_yaml = "\n".join(f"    - {item}" for item in secrets)
    manifest = f"""
plugin:
  id: {module_name}
  name: Wiring Test Plugin
  version: 1.0.0
  author: tests
  language: {language}
  tools:
    - name: context_probe
      module: {module_name}
      description: Probe production context
      permissions:
{permission_yaml or '        []'}
{extra_tool_yaml}
  secrets:
{secret_yaml or '    []'}
{extra_plugin_yaml}
"""
    (plugin_dir / "plugin.yaml").write_text(
        textwrap.dedent(manifest).strip() + "\n",
        encoding="utf-8",
    )
    return plugin_dir


@pytest.fixture(autouse=True)
def _reset_plugin_state():
    original_path = list(sys.path)
    original_tools = dict(tool_registry._TOOL_MAP)
    loader._loaded_plugins = []
    try:
        yield
    finally:
        loader._loaded_plugins = []
        tool_registry._TOOL_MAP.clear()
        tool_registry._TOOL_MAP.update(original_tools)
        sys.path[:] = original_path


def _set_identity(*permissions: str):
    tenant_token = tenant_context.set("tenant-a")
    auth_token = set_active_auth_context(
        AuthContext(
            user_id="alice",
            tenant_id="tenant-a",
            permissions=list(permissions),
            roles=["ROLE_ADMIN"],
        )
    )
    set_request_context(
        "request-1",
        "run-1",
        "tenant-a:session-42",
        workspace_root="/tmp/run-workspace",
    )
    set_agent_contract("coding-agent", "project-docs")
    return tenant_token, auth_token


def _reset_identity(tokens) -> None:
    tenant_token, auth_token = tokens
    reset_active_auth_context(auth_token)
    tenant_context.reset(tenant_token)


def test_loaded_plugin_receives_real_request_context(tmp_path, monkeypatch):
    plugin_dir = _write_plugin(
        tmp_path,
        permissions=("plugin:use",),
        secrets=("PLUGIN_TOKEN",),
        body="""
            import json
            from agent_sdk import ToolContext, tool

            @tool(name="context_probe", description="probe", permissions=["plugin:use"])
            def context_probe(ctx: ToolContext) -> str:
                return json.dumps({
                    "tenant_id": ctx.tenant_id,
                    "session_id": ctx.session_id,
                    "user_role": ctx.user_role,
                    "workspace": ctx.workspace,
                    "secret": ctx.secrets["PLUGIN_TOKEN"],
                })
        """,
    )
    monkeypatch.setenv("PLUGIN_TOKEN", "secret-value")
    tokens = _set_identity("plugin:use")
    try:
        loader.load_plugins(str(tmp_path))
        [plugin] = loader.get_plugin_tools()
        assert tool_registry.resolve_tools(["context_probe"], strict=True) == [plugin]
        observed = json.loads(plugin.invoke({}))
    finally:
        _reset_identity(tokens)

    assert observed == {
        "tenant_id": "tenant-a",
        "session_id": "tenant-a:session-42",
        "user_role": "ADMIN",
        "workspace": "/tmp/run-workspace",
        "secret": "secret-value",
    }
    assert plugin_dir.exists()


def test_plugin_permission_denies_before_function_body(tmp_path):
    marker = tmp_path / "executed.txt"
    _write_plugin(
        tmp_path,
        permissions=("plugin:admin",),
        body=f"""
            from pathlib import Path
            from agent_sdk import ToolContext, tool

            @tool(name="context_probe", description="probe", permissions=["plugin:admin"])
            def context_probe(ctx: ToolContext) -> str:
                Path({str(marker)!r}).write_text("executed")
                return "executed"
        """,
    )
    tokens = _set_identity("plugin:read")
    try:
        loader.load_plugins(str(tmp_path))
        [plugin] = loader.get_plugin_tools()
        with pytest.raises(PermissionError, match="plugin:admin"):
            plugin.invoke({})
    finally:
        _reset_identity(tokens)

    assert not marker.exists()


def test_plugin_with_missing_required_secret_is_not_registered(tmp_path):
    _write_plugin(
        tmp_path,
        secrets=("MISSING_PLUGIN_SECRET",),
        body="""
            from agent_sdk import ToolContext, tool

            @tool(name="context_probe", description="probe")
            def context_probe(ctx: ToolContext) -> str:
                return "unexpected"
        """,
    )

    assert loader.load_plugins(str(tmp_path)) == []
    assert loader.get_plugin_tools() == []


def test_plugin_with_empty_required_secret_is_not_registered(tmp_path, monkeypatch):
    _write_plugin(
        tmp_path,
        secrets=("EMPTY_PLUGIN_SECRET",),
        body="""
            from agent_sdk import ToolContext, tool

            @tool(name="context_probe", description="probe")
            def context_probe(ctx: ToolContext) -> str:
                return "unexpected"
        """,
    )
    monkeypatch.setenv("EMPTY_PLUGIN_SECRET", "")

    assert loader.load_plugins(str(tmp_path)) == []
    assert loader.get_plugin_tools() == []


def test_manifest_tool_without_implementation_is_not_loaded(tmp_path):
    _write_plugin(
        tmp_path,
        body="""
            from agent_sdk import ToolContext, tool

            @tool(name="different_tool", description="not the declared tool")
            def different_tool(ctx: ToolContext) -> str:
                return "unexpected"
        """,
    )

    assert loader.load_plugins(str(tmp_path)) == []
    assert loader.get_plugin_tools() == []


def test_registry_failure_is_explicit_and_clears_loaded_plugins(tmp_path, monkeypatch):
    _write_plugin(
        tmp_path,
        body="""
            from agent_sdk import ToolContext, tool

            @tool(name="context_probe", description="probe")
            def context_probe(ctx: ToolContext) -> str:
                return "unexpected"
        """,
    )

    def fail_registration(_tool):
        raise RuntimeError("registry unavailable")

    monkeypatch.setattr(tool_registry, "register_tool", fail_registration)

    with pytest.raises(RuntimeError, match="registry unavailable"):
        loader.load_plugins(str(tmp_path))
    assert loader.get_plugin_tools() == []


@pytest.mark.parametrize(
    ("language", "extra_tool_yaml", "extra_plugin_yaml"),
    [
        ("java", "", ""),
        ("python", "      class: ProbeTool", ""),
        ("python", "      config:\n        mode: strict", ""),
        ("python", "", "  knowledge:\n    - docs/runbooks"),
    ],
)
def test_unsupported_manifest_capabilities_fail_closed(
    tmp_path,
    language,
    extra_tool_yaml,
    extra_plugin_yaml,
):
    _write_plugin(
        tmp_path,
        language=language,
        extra_tool_yaml=extra_tool_yaml,
        extra_plugin_yaml=extra_plugin_yaml,
        body="""
            from agent_sdk import ToolContext, tool

            @tool(name="context_probe", description="probe")
            def context_probe(ctx: ToolContext) -> str:
                return "unexpected"
        """,
    )

    assert loader.load_plugins(str(tmp_path)) == []
    assert loader.get_plugin_tools() == []


def test_production_clients_use_request_tenant_and_collection(monkeypatch):
    calls = []

    class Retriever:
        async def search(self, query: str, top_k: int = 5):
            calls.append(("knowledge", query, top_k))
            return [
                SimpleNamespace(
                    content="architecture",
                    file_path="docs/architecture.md",
                    name="Architecture",
                    score=0.95,
                )
            ]

    async def get_retriever(tenant_id: str, collection: str | None = None):
        calls.append(("retriever", tenant_id, collection))
        return Retriever()

    class ToolClient:
        async def call_tool(self, name: str, arguments: dict, tenant_id: str):
            calls.append(("memory", name, arguments, tenant_id))
            if name == "memory_get":
                return '{"key":"k","content":"remembered"}'
            raise AssertionError(name)

    monkeypatch.setattr(
        "app.rag.index.retriever.get_retriever_for_tenant",
        get_retriever,
    )
    monkeypatch.setattr("app.tools.definitions._get_memory", lambda: ToolClient())
    tokens = _set_identity("plugin:use")
    try:
        knowledge = loader._build_knowledge_client()
        memory = loader._build_memory_client()
        knowledge_results = __import__("asyncio").run(knowledge.search("arch", 2))
        memory_result = __import__("asyncio").run(memory.get("k"))
    finally:
        _reset_identity(tokens)

    assert knowledge_results[0].file_path == "docs/architecture.md"
    assert memory_result == "remembered"
    assert calls == [
        ("retriever", "tenant-a", "project-docs"),
        ("knowledge", "arch", 2),
        ("memory", "memory_get", {"key": "k"}, "tenant-a"),
    ]
