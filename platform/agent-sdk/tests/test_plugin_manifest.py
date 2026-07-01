"""Tests for the plugin manifest parser."""

from pathlib import Path

import pytest

from agent_sdk.plugin import load_plugin_manifest


def test_load_valid_manifest(tmp_path: Path) -> None:
    """A valid manifest should parse into a PluginManifest."""

    manifest_path = tmp_path / "plugin.yaml"
    manifest_path.write_text(
        """
plugin:
  id: sample-plugin
  name: Sample Plugin
  version: 1.0.0
  author: Example Author
  language: python
  tools:
    - name: ticket_create
      module: sample_plugin.tools
      class: TicketTool
      description: Create a ticket
      permissions:
        - tickets:write
      config:
        project: ENG
  secrets:
    - JIRA_TOKEN
  knowledge:
    - docs/runbooks
""".strip(),
        encoding="utf-8",
    )

    manifest = load_plugin_manifest(manifest_path)

    assert manifest.id == "sample-plugin"
    assert manifest.name == "Sample Plugin"
    assert manifest.version == "1.0.0"
    assert manifest.author == "Example Author"
    assert manifest.language == "python"
    assert len(manifest.tools) == 1
    assert manifest.tools[0].name == "ticket_create"
    assert manifest.tools[0].module == "sample_plugin.tools"
    assert manifest.tools[0].class_name == "TicketTool"
    assert manifest.tools[0].permissions == ["tickets:write"]
    assert manifest.secrets == ["JIRA_TOKEN"]
    assert manifest.knowledge == ["docs/runbooks"]


def test_load_missing_required_field(tmp_path: Path) -> None:
    """Missing required plugin fields should raise a clear ValueError."""

    manifest_path = tmp_path / "plugin.yaml"
    manifest_path.write_text(
        """
plugin:
  name: Sample Plugin
  version: 1.0.0
  author: Example Author
  language: python
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="missing required field 'id'"):
        load_plugin_manifest(manifest_path)
