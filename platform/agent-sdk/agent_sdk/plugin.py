"""Plugin manifest (plugin.yaml) parser and validator."""

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class ToolEntry:
    """A tool declared in a plugin manifest."""

    name: str
    module: str | None = None
    class_name: str | None = None
    description: str = ""
    permissions: list[str] = field(default_factory=list)
    config: dict | None = None


@dataclass
class PluginManifest:
    """Parsed plugin.yaml manifest."""

    id: str
    name: str
    version: str
    author: str
    language: str
    tools: list[ToolEntry] = field(default_factory=list)
    secrets: list[str] = field(default_factory=list)
    knowledge: list[str] = field(default_factory=list)


def load_plugin_manifest(path: str | Path) -> PluginManifest:
    """Load and validate a plugin.yaml manifest file.

    Raises ValueError for missing required fields.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Plugin manifest not found: {path}")

    with open(path) as f:
        raw = yaml.safe_load(f)

    if not raw or "plugin" not in raw:
        raise ValueError(f"Invalid plugin manifest: missing 'plugin' key in {path}")

    plugin = raw["plugin"]

    required = ["id", "name", "version", "author", "language"]
    for field_name in required:
        if field_name not in plugin:
            raise ValueError(
                f"Invalid plugin manifest {path}: missing required field '{field_name}'"
            )

    tools = []
    for t in plugin.get("tools", []):
        if "name" not in t:
            raise ValueError(f"Tool entry missing 'name' in {path}")
        tools.append(
            ToolEntry(
                name=t["name"],
                module=t.get("module"),
                class_name=t.get("class"),
                description=t.get("description", ""),
                permissions=t.get("permissions", []),
                config=t.get("config"),
            )
        )

    return PluginManifest(
        id=plugin["id"],
        name=plugin["name"],
        version=plugin["version"],
        author=plugin["author"],
        language=plugin["language"],
        tools=tools,
        secrets=plugin.get("secrets", []),
        knowledge=plugin.get("knowledge", []),
    )
