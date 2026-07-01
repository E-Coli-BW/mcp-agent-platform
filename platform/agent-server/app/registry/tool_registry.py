"""Tool registry — resolves tool names from config to LangChain tool objects."""

import logging
import os

from langchain_core.tools import BaseTool

logger = logging.getLogger(__name__)

# All known tools indexed by name
_TOOL_MAP: dict[str, BaseTool] = {}


def register_tool(tool: BaseTool) -> None:
    """Register a tool by its name."""
    _TOOL_MAP[tool.name] = tool


class UnknownToolError(RuntimeError):
    """Raised by resolve_tools(strict=True) when an agent config references
    a tool that hasn't been registered. This usually means a backend service
    (memory-server, codeexec-server, ...) is down, or a plugin failed to load.

    We intentionally fail loud — silently dropping tools from an agent's
    declared capability set leads to mystery degradations where the agent
    appears to work but is missing 1/3 of its tools without any error.
    """


def resolve_tools(names: list[str], *, strict: bool | None = None) -> list[BaseTool]:
    """Resolve a list of tool names to tool objects.

    Args:
        names: Tool names from an agent config (yaml `tools:` list).
        strict: If True, raise :class:`UnknownToolError` when any name is
            missing instead of silently dropping it. If None (default),
            read from env var ``AGENT_STRICT_TOOLS`` (defaults to ``true`` —
            we prefer fail-loud since silent drops caused real outages).

    The yaml -> registry -> agent path is the main place we'd silently lose
    capabilities. The failure mode is hard to spot from production logs
    (it's just a WARNING line at startup), so strict mode prevents the
    server from booting at all when the config and the runtime disagree.
    """
    if strict is None:
        strict = os.environ.get("AGENT_STRICT_TOOLS", "true").lower() in (
            "1",
            "true",
            "yes",
            "on",
        )

    resolved: list[BaseTool] = []
    missing: list[str] = []
    for name in names:
        if name in _TOOL_MAP:
            resolved.append(_TOOL_MAP[name])
        else:
            missing.append(name)

    if missing:
        msg = (
            f"Agent config references {len(missing)} unregistered tool(s): "
            f"{', '.join(missing)}. "
            f"Available tools: {sorted(_TOOL_MAP.keys())}. "
            "This usually means a backend service is down (memory-server / "
            "codeexec-server) or a plugin failed to load. "
            "Start the missing service, or set AGENT_STRICT_TOOLS=false to "
            "boot with a degraded tool set."
        )
        if strict:
            logger.error("❌ %s", msg)
            raise UnknownToolError(msg)
        for name in missing:
            logger.warning("Unknown tool in config: %s", name)

    return resolved


def get_registered_tools() -> dict[str, BaseTool]:
    """Return the full tool map (read-only access for introspection)."""
    return dict(_TOOL_MAP)


def register_all_builtins() -> None:
    """Register all builtin tools from definitions.py.

    Called at startup to populate the registry with all known tools.
    """
    from app.tools.definitions import get_tools

    for tool in get_tools():
        register_tool(tool)
    logger.info("Registered %d builtin tools in registry", len(_TOOL_MAP))
