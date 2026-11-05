"""Plugin loader — discovers and loads tool plugins from a directory."""

import importlib
import inspect
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from langchain_core.tools import tool as langchain_tool

logger = logging.getLogger(__name__)


@dataclass
class LoadedPlugin:
    """A successfully loaded plugin."""

    id: str
    name: str
    version: str
    tools: list


_loaded_plugins: list[LoadedPlugin] = []


def load_plugins(plugins_dir: str) -> list[LoadedPlugin]:
    """Load all plugins from the given directory.

    Each plugin is a subdirectory containing a plugin.yaml manifest.
    Handles missing directory gracefully (logs warning, continues).
    """
    global _loaded_plugins
    _loaded_plugins = []

    plugins_path = Path(plugins_dir)
    if not plugins_path.exists():
        logger.warning(
            "⚠️  Plugins directory not found: %s — skipping plugin loading", plugins_dir
        )
        _invalidate_tool_cache()
        return []

    if not plugins_path.is_dir():
        logger.warning("⚠️  Plugins path is not a directory: %s", plugins_dir)
        _invalidate_tool_cache()
        return []

    for entry in sorted(plugins_path.iterdir()):
        if not entry.is_dir():
            continue
        manifest_path = entry / "plugin.yaml"
        if not manifest_path.exists():
            continue

        try:
            _load_single_plugin(entry, manifest_path)
        except Exception as e:  # noqa: BLE001  # pragma: no cover - plugin isolation
            logger.error("❌ Failed to load plugin from %s: %s", entry.name, e)

    try:
        _register_plugin_tools()
        _invalidate_tool_cache()
    except Exception:
        _loaded_plugins = []
        raise
    logger.info(
        "✅ Loaded %d plugin(s) with %d total tools",
        len(_loaded_plugins),
        sum(len(plugin.tools) for plugin in _loaded_plugins),
    )
    return _loaded_plugins


def _load_single_plugin(plugin_dir: Path, manifest_path: Path) -> None:
    """Load a single plugin from its directory."""
    from agent_sdk.plugin import load_plugin_manifest

    manifest = load_plugin_manifest(str(manifest_path))

    if manifest.language.lower() != "python":
        raise ValueError(f"Unsupported plugin language: {manifest.language}")
    if manifest.knowledge:
        raise ValueError("Plugin manifest knowledge declarations are not supported")
    for tool_entry in manifest.tools:
        if tool_entry.class_name:
            raise ValueError("Class-based plugin tools are not supported")
        if tool_entry.config is not None:
            raise ValueError("Per-tool plugin config is not supported")

    plugin_dir_str = str(plugin_dir)
    if plugin_dir_str not in sys.path:
        sys.path.insert(0, plugin_dir_str)

    missing_secrets = [secret for secret in manifest.secrets if not os.environ.get(secret)]
    if missing_secrets:
        raise ValueError(
            f"Plugin {manifest.name} missing required secrets: {missing_secrets}"
        )

    loaded_tools = []
    for tool_entry in manifest.tools:
        if not tool_entry.module:
            raise ValueError(f"Plugin tool {tool_entry.name} has no module specified")

        try:
            module = importlib.import_module(tool_entry.module)
        except ImportError as e:
            raise ImportError(f"Failed to import plugin module {tool_entry.module}") from e

        tool_func = None
        for attr_name in dir(module):
            attr = getattr(module, attr_name)
            if (
                callable(attr)
                and hasattr(attr, "_tool_metadata")
                and attr._tool_metadata.name == tool_entry.name
            ):
                tool_func = attr
                break

        if tool_func is None:
            raise ValueError(
                f"Tool function {tool_entry.name!r} not found in module {tool_entry.module}"
            )

        required_permissions = sorted(
            set(tool_entry.permissions) | set(tool_func._tool_metadata.permissions)
        )
        loaded_tools.append(
            _wrap_as_langchain_tool(
                tool_func,
                manifest.secrets,
                required_permissions=required_permissions,
            )
        )

    plugin = LoadedPlugin(
        id=manifest.id,
        name=manifest.name,
        version=manifest.version,
        tools=loaded_tools,
    )
    _loaded_plugins.append(plugin)
    logger.info(
        "✅ Loaded plugin %s v%s: %d tools",
        manifest.name,
        manifest.version,
        len(loaded_tools),
    )


def _wrap_as_langchain_tool(
    tool_func,
    required_secrets: list[str],
    required_permissions: list[str] | None = None,
):
    """Wrap a plugin tool function as a LangChain tool."""
    from agent_sdk.context import ToolContext

    from app.auth.middleware import get_active_auth_context, tenant_context
    from app.context.request_context import get_request_context

    metadata = tool_func._tool_metadata
    signature = inspect.signature(tool_func)
    parameters = list(signature.parameters.values())
    if not parameters:
        msg = "Plugin tools must accept ToolContext as the first parameter"
        raise ValueError(msg)

    context_param = parameters[0].name
    tool_signature = signature.replace(parameters=parameters[1:])
    tool_annotations = {
        name: annotation
        for name, annotation in getattr(tool_func, "__annotations__", {}).items()
        if name != context_param
    }

    required_permissions = required_permissions or []

    def build_context() -> ToolContext:
        auth = get_active_auth_context()
        if auth is None:
            raise PermissionError("Plugin tools require an authenticated request context")
        missing_permissions = [
            permission
            for permission in required_permissions
            if permission not in auth.permissions and "*" not in auth.permissions
        ]
        if missing_permissions:
            raise PermissionError(
                "Plugin tool requires permission(s): " + ", ".join(missing_permissions)
            )
        request = get_request_context()
        if not request.session_id or not request.workspace_root:
            raise RuntimeError("Plugin tools require active session and workspace context")
        missing_secrets = [key for key in required_secrets if not os.environ.get(key)]
        if missing_secrets:
            raise RuntimeError(
                "Plugin required secrets became unavailable: " + ", ".join(missing_secrets)
            )
        role = "USER"
        if auth.roles:
            role = auth.roles[0].removeprefix("ROLE_")
        elif "*" in auth.permissions:
            role = "ADMIN"
        return ToolContext(
            tenant_id=tenant_context.get(),
            session_id=request.session_id,
            user_role=role,
            memory=_build_memory_client(),
            knowledge=_build_knowledge_client(),
            secrets={key: os.environ.get(key, "") for key in required_secrets},
            workspace=request.workspace_root,
        )

    if inspect.iscoroutinefunction(tool_func):

        async def plugin_tool(*args, **kwargs) -> str:
            ctx = build_context()
            return await tool_func(ctx, *args, **kwargs)

    else:

        def plugin_tool(*args, **kwargs) -> str:
            ctx = build_context()
            return tool_func(ctx, *args, **kwargs)

    plugin_tool.__name__ = metadata.name
    plugin_tool.__doc__ = metadata.description or (tool_func.__doc__ or metadata.name)
    plugin_tool.__signature__ = tool_signature
    plugin_tool.__annotations__ = tool_annotations

    wrapped = langchain_tool(metadata.name, description=metadata.description)(plugin_tool)
    wrapped.metadata = {
        "plugin_required_permissions": list(required_permissions),
    }
    return wrapped


def _build_memory_client():
    """Build an SDK client over the authenticated Agent-to-Memory transport."""
    from agent_sdk.memory import MemoryClient

    async def call_tool(tool_name: str, arguments: dict) -> str:
        from app.tools.definitions import _get_memory

        return await _get_memory().call_tool(
            tool_name,
            arguments,
            tenant_id=__import__("app.auth.middleware", fromlist=["tenant_context"])
            .tenant_context.get(),
        )

    return MemoryClient(tool_caller=call_tool)


def _build_knowledge_client():
    """Build a tenant/collection-aware SDK knowledge client."""
    from agent_sdk.knowledge import KnowledgeBaseClient

    async def get_retriever():
        from app.auth.middleware import tenant_context
        from app.context.request_context import get_request_context
        from app.rag.index.retriever import get_retriever_for_tenant

        request = get_request_context()
        return await get_retriever_for_tenant(
            tenant_context.get(),
            collection=request.knowledge_collection or None,
        )

    return KnowledgeBaseClient(retriever_factory=get_retriever)


def _register_plugin_tools() -> None:
    """Register loaded plugin tools so YAML agent configs can resolve them."""
    from app.registry.tool_registry import register_tool

    for plugin in _loaded_plugins:
        for tool in plugin.tools:
            register_tool(tool)


def _invalidate_tool_cache() -> None:
    """Invalidate the cached tool list so newly loaded plugins appear immediately."""
    from app.tools import definitions

    definitions._cached_tools = None
    definitions._cache_timestamp = 0


def get_plugin_tools() -> list:
    """Return all loaded plugin tools as a flat list."""
    tools = []
    for plugin in _loaded_plugins:
        tools.extend(plugin.tools)
    return tools
