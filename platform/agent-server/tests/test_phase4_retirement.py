from __future__ import annotations

from pathlib import Path

from app.config import settings
from app.events.model_provenance import build_feature_flags_snapshot

AGENT_SERVER_ROOT = Path(__file__).parents[1]
PLATFORM_ROOT = AGENT_SERVER_ROOT.parent
REPO_ROOT = PLATFORM_ROOT.parent
GRAPH_VERSION_FIELD = "agent_" + "graph_version"
LEGACY_FALLBACK_FILE = "model_" + "fallback.py"


def test_legacy_graph_runtime_is_not_configurable_or_importable():
    agent_dir = AGENT_SERVER_ROOT / "app" / "agent"

    assert not hasattr(settings, GRAPH_VERSION_FIELD)
    assert GRAPH_VERSION_FIELD not in build_feature_flags_snapshot()
    assert not (agent_dir / "graph.py").exists()
    assert not (agent_dir / "graph_v2.py").exists()
    assert not (agent_dir / LEGACY_FALLBACK_FILE).exists()

    registry_source = (agent_dir / "engine" / "registry.py").read_text()
    assert "create_react_agent" not in registry_source
    assert GRAPH_VERSION_FIELD not in registry_source


def test_active_runtime_surfaces_have_no_legacy_graph_references():
    active_files = [
        *(AGENT_SERVER_ROOT / "app").rglob("*.py"),
        *(AGENT_SERVER_ROOT / "tests").rglob("*.py"),
        *(PLATFORM_ROOT / "eval-harness").glob("*.py"),
        *(PLATFORM_ROOT / "eval-harness").glob("*.sh"),
        PLATFORM_ROOT / "eval-harness" / "JAVA_VS_PYTHON.md",
        PLATFORM_ROOT / "docs" / "RUNTIME-REFACTOR-ROADMAP.md",
        PLATFORM_ROOT / "docs" / "design" / "AGENT-SERVER-DESIGN.md",
        PLATFORM_ROOT
        / "docs"
        / "design"
        / "PROMPT-GOVERNANCE-PROVENANCE-IMPLEMENTATION.md",
        AGENT_SERVER_ROOT / "docs" / "OBSERVABILITY.md",
    ]
    forbidden_tokens = (
        "AGENT_" + "AGENT_GRAPH_VERSION",
        "agent_" + "graph_version",
        "app.agent." + "graph_v2",
        "app.agent." + "graph import",
        "python-" + "v1",
        "model_" + "fallback",
    )
    violations = []
    for path in active_files:
        content = path.read_text(errors="replace")
        for token in forbidden_tokens:
            if token in content:
                violations.append(f"{path.relative_to(REPO_ROOT)} contains {token}")

    assert violations == []
