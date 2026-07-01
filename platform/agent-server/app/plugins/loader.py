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
        except Exception as e:  # pragma: no cover - defensive logging path
            logger.error("❌ Failed to load plugin from %s: %s", entry.name, e)

    _register_plugin_tools()
    _invalidate_tool_cache()
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

    plugin_dir_str = str(plugin_dir)
    if plugin_dir_str not in sys.path:
        sys.path.insert(0, plugin_dir_str)

    missing_secrets = [secret for secret in manifest.secrets if secret not in os.environ]
    if missing_secrets:
        logger.warning(
            "⚠️  Plugin %s missing secrets: %s — loading anyway",
            manifest.name,
            missing_secrets,
        )

    loaded_tools = []
    for tool_entry in manifest.tools:
        if not tool_entry.module:
            logger.warning("Tool %s has no module specified, skipping", tool_entry.name)
            continue

        try:
            module = importlib.import_module(tool_entry.module)
        except ImportError as e:
            logger.error("Failed to import module %s: %s", tool_entry.module, e)
            continue

        tool_func = None
        for attr_name in dir(module):
            attr = getattr(module, attr_name)
            if callable(attr) and hasattr(attr, "_tool_metadata"):
                if attr._tool_metadata.name == tool_entry.name:
                    tool_func = attr
                    break

        if tool_func is None:
            logger.warning(
                "Tool function '%s' not found in module %s",
                tool_entry.name,
                tool_entry.module,
            )
            continue

        loaded_tools.append(_wrap_as_langchain_tool(tool_func, manifest.secrets))

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


def _wrap_as_langchain_tool(tool_func, required_secrets: list[str]):
    """Wrap a plugin tool function as a LangChain tool."""
    from app.auth.middleware import tenant_context
    from agent_sdk.context import ToolContext

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

    def build_context(session_id: str = "unknown", user_role: str = "USER") -> ToolContext:
        return ToolContext(
            tenant_id=tenant_context.get(),
            session_id=session_id,
            user_role=user_role,
            memory=_build_memory_client(),
            knowledge=_build_knowledge_client(),
            secrets={key: os.environ.get(key, "") for key in required_secrets},
            workspace=os.environ.get("AGENT_WORKSPACE", os.getcwd()),
        )

    if inspect.iscoroutinefunction(tool_func):

        async def plugin_tool(*args, **kwargs) -> str:
            session_id = kwargs.pop("session_id", "unknown")
            user_role = kwargs.pop("user_role", "USER")
            ctx = build_context(session_id=session_id, user_role=user_role)
            return await tool_func(ctx, *args, **kwargs)

    else:

        def plugin_tool(*args, **kwargs) -> str:
            session_id = kwargs.pop("session_id", "unknown")
            user_role = kwargs.pop("user_role", "USER")
            ctx = build_context(session_id=session_id, user_role=user_role)
            return tool_func(ctx, *args, **kwargs)

    plugin_tool.__name__ = metadata.name
    plugin_tool.__doc__ = metadata.description or (tool_func.__doc__ or metadata.name)
    plugin_tool.__signature__ = tool_signature
    plugin_tool.__annotations__ = tool_annotations

    return langchain_tool(metadata.name, description=metadata.description)(plugin_tool)


def _build_memory_client():
    """Build the optional SDK memory client if the SDK provides one."""
    try:
        from agent_sdk.memory import MemoryClient

        return MemoryClient(
            base_url=os.environ.get("MEMORY_SERVER_URL", "http://localhost:8180"),
            auth_token="",
        )
    except Exception as e:  # pragma: no cover - depends on optional SDK modules
        logger.debug("Memory client unavailable for plugin context: %s", e)
        return None


def _build_knowledge_client():
    """Build the optional SDK knowledge client if the SDK provides one."""
    try:
        from agent_sdk.knowledge import KnowledgeBaseClient

        return KnowledgeBaseClient()
    except Exception as e:  # pragma: no cover - depends on optional SDK modules
        logger.debug("Knowledge client unavailable for plugin context: %s", e)
        return None


def _register_plugin_tools() -> None:
    """Register loaded plugin tools so YAML agent configs can resolve them."""
    try:
        from app.registry.tool_registry import register_tool

        for plugin in _loaded_plugins:
            for tool in plugin.tools:
                register_tool(tool)
    except Exception as e:  # pragma: no cover - defensive logging path
        logger.warning("Failed to register plugin tools: %s", e)


def _invalidate_tool_cache() -> None:
    """Invalidate the cached tool list so newly loaded plugins appear immediately."""
    try:
        from app.tools import definitions

        definitions._cached_tools = None
        definitions._cache_timestamp = 0
    except Exception as e:  # pragma: no cover - defensive logging path
        logger.debug("Failed to invalidate tool cache: %s", e)


def get_plugin_tools() -> list:
    """Return all loaded plugin tools as a flat list."""
    tools = []
    for plugin in _loaded_plugins:
        tools.extend(plugin.tools)
    return tools
