"""Workspace context — auto-detects project type and key files.

Injects project metadata into the system prompt so the agent knows
what kind of project it's in without needing to call file_list first.
"""

import os
from pathlib import Path

# Project type detection by marker files
PROJECT_MARKERS = {
    "pom.xml": "Java/Maven",
    "build.gradle": "Java/Gradle",
    "package.json": "Node.js",
    "pyproject.toml": "Python",
    "Cargo.toml": "Rust",
    "go.mod": "Go",
    "Gemfile": "Ruby",
    "requirements.txt": "Python",
}

# Key files to read for summary
KEY_FILES = [
    "README.md",
    "README",
    "README.rst",
    "package.json",
    "pyproject.toml",
    "pom.xml",
]

# Module detection patterns
MODULE_MARKERS = {
    "pom.xml",
    "package.json",
    "pyproject.toml",
    "build.gradle",
    "Cargo.toml",
}

# Dirs to skip during module scanning
_SKIP_DIRS = {
    ".git",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    "target",
    "dist",
    "build",
    ".idea",
    ".tox",
    "egg-info",
}

# Module-level cache
_cache: tuple[str, str] | None = None


def detect_project_type(root: str) -> str | None:
    """Detect project type from marker files in the root directory."""
    for marker, ptype in PROJECT_MARKERS.items():
        if os.path.exists(os.path.join(root, marker)):
            return ptype
    return None


def detect_modules(root: str, max_depth: int = 2) -> list[str]:
    """Detect sub-modules (mono-repo detection).

    Walks directories up to max_depth looking for module marker files.
    Returns relative paths of discovered sub-modules.
    """
    modules = []
    root_path = Path(root)
    for dirpath, dirnames, filenames in os.walk(root):
        depth = len(Path(dirpath).relative_to(root_path).parts)
        if depth > max_depth:
            dirnames.clear()
            continue
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for marker in MODULE_MARKERS:
            if marker in filenames and dirpath != root:
                rel = os.path.relpath(dirpath, root)
                modules.append(rel)
                break
    return modules


def read_summary_file(root: str, max_chars: int = 500) -> str | None:
    """Read the first N chars of README or similar project summary."""
    for name in KEY_FILES:
        path = os.path.join(root, name)
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read(max_chars)
                return f"[{name}]: {content.strip()}"
            except OSError:
                pass
    return None


def get_workspace_context(root: str) -> str:
    """Generate a workspace context string for the system prompt.

    Returns a multi-line string with project type, modules, and summary.
    Uses module-level caching — invalidates when root changes.
    """
    global _cache
    if _cache and _cache[0] == root:
        return _cache[1]

    parts = [f"Workspace: {os.path.basename(root)}"]

    ptype = detect_project_type(root)
    if ptype:
        parts.append(f"Type: {ptype}")

    modules = detect_modules(root)
    if modules:
        parts.append(f"Modules: {', '.join(modules[:10])}")
        if len(modules) > 10:
            parts.append(f"  ... and {len(modules) - 10} more")

    summary = read_summary_file(root)
    if summary:
        parts.append(summary)

    ctx = "\n".join(parts)
    _cache = (root, ctx)
    return ctx
