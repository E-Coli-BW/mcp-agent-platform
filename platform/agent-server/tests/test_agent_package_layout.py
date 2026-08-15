from __future__ import annotations

import ast
from pathlib import Path

AGENT_SERVER_ROOT = Path(__file__).parents[1]
AGENT_ROOT = AGENT_SERVER_ROOT / "app" / "agent"

EXPECTED_PACKAGES = {
    "agent_graph",
    "compressor",
    "delegation",
    "engine",
    "execution_policy",
    "intent",
    "model",
    "skills",
    "tool_routing",
}

RETIRED_ROOT_MODULES = {
    "checkpointing",
    "classifier_eval",
    "fleet_bus",
    "model_recovery",
    "prompts",
    "reflexion",
    "runtime_profile",
    "skill_activation",
    "skills_index",
    "subagent",
    "subagent_context",
    "subagent_verifier",
    "tool_contract",
    "tool_router",
}


def test_agent_root_exposes_only_domain_packages():
    direct_files = {path.name for path in AGENT_ROOT.iterdir() if path.is_file()}
    direct_packages = {
        path.name
        for path in AGENT_ROOT.iterdir()
        if path.is_dir() and not path.name.startswith("__")
    }

    assert direct_files == {"__init__.py"}
    assert direct_packages == EXPECTED_PACKAGES


def test_python_sources_do_not_import_retired_agent_root_modules():
    source_roots = [
        AGENT_SERVER_ROOT / "app",
        AGENT_SERVER_ROOT / "tests",
        AGENT_SERVER_ROOT / "scripts",
    ]
    violations = []
    for source_root in source_roots:
        for source_path in source_root.rglob("*.py"):
            tree = ast.parse(source_path.read_text())
            imported_modules = []
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported_modules.extend(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported_modules.append(node.module)

            content = source_path.read_text()
            for retired_name in RETIRED_ROOT_MODULES:
                retired_module = f"app.agent.{retired_name}"
                if any(
                    module == retired_module
                    or module.startswith(retired_module + ".")
                    for module in imported_modules
                ) or retired_module in content:
                    violations.append(
                        f"{source_path.relative_to(AGENT_SERVER_ROOT)} references "
                        f"{retired_module}"
                    )

    assert violations == []


def test_tool_routing_policy_has_no_graph_framework_dependency():
    violations = []
    for source_path in (AGENT_ROOT / "tool_routing").rglob("*.py"):
        tree = ast.parse(source_path.read_text())
        for node in ast.walk(tree):
            modules = []
            if isinstance(node, ast.Import):
                modules.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules.append(node.module)
            for module in modules:
                if module.startswith(("langgraph", "langchain")):
                    violations.append(
                        f"{source_path.relative_to(AGENT_SERVER_ROOT)} imports {module}"
                    )

    assert violations == []