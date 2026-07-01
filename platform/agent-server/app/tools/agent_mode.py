"""Agent mode tools — file writing, editing, and git operations.

These tools give the agent the ability to ACT, not just READ.
They use the code_shell tool under the hood for execution.

Security model (review findings C1 + C2):
    * Workspace is resolved per-tenant via :mod:`app.tools.workspace_resolver`.
      The tenant id comes from the verified JWT (``tenant_context`` ContextVar).
    * Every path argument is validated via
      :func:`app.tools.workspace_resolver.validate_path`, which uses
      ``Path.resolve()`` + ``is_relative_to()`` (not ``startswith``) and refuses
      symlinks pointing outside the workspace.
    * Absolute paths from the agent are refused; the agent only ever names
      files relative to its tenant workspace.
"""

import os
from pathlib import Path
from langchain_core.tools import tool

from app.tools.workspace_resolver import (
    WorkspacePathError,
    get_resolver,
    validate_path,
)


def _current_tenant_id() -> str | None:
    """Best-effort tenant lookup. Returns None if auth middleware isn't loaded
    (e.g. unit tests importing this module in isolation).
    """
    try:
        from app.auth.middleware import tenant_context  # local import — avoids cycle at import time

        return tenant_context.get()
    except Exception:
        return None


def get_workspace_root() -> str:
    """Return the workspace directory for the current tenant.

    Multi-tenant mode (``AGENT_MULTI_TENANT_WORKSPACE=1``) gives each tenant
    a subdirectory under the configured base. Single-tenant / dev mode (the
    default) returns the same directory for every caller so existing tests
    that call ``set_workspace_root(tmp_path)`` keep working.
    """
    return get_resolver().for_tenant(_current_tenant_id())


def set_workspace_root(path: str) -> None:
    """Pin the workspace directory for the current tenant.

    In single-tenant mode this updates the shared base for every caller — the
    behavior the legacy global variable had. In multi-tenant mode it scopes the
    override to the calling tenant only.
    """
    resolver = get_resolver()
    tenant = _current_tenant_id()
    if resolver.is_multi_tenant():
        resolver.set_for_tenant(tenant, path)
    else:
        resolver.set_base(path)


def _ensure_workspace() -> str:
    """Create the current tenant's workspace directory if needed and return it."""
    root = get_workspace_root()
    Path(root).mkdir(parents=True, exist_ok=True)
    return root


def _safe_workspace_path(path: str) -> str:
    """Resolve ``path`` inside the current tenant's workspace.

    Raises :class:`WorkspacePathError` for any traversal / symlink escape /
    absolute-path attempt. Tool callers translate this into a user-facing
    error string at the boundary.
    """
    root = _ensure_workspace()
    return validate_path(path, root)


@tool
def file_write(path: str, content: str) -> str:
    """Write content to a file. Creates parent directories if needed. Path is relative to workspace root."""
    try:
        resolved = _safe_workspace_path(path)
        os.makedirs(os.path.dirname(resolved), exist_ok=True)
        with open(resolved, 'w') as f:
            f.write(content)
        lines = content.count('\n') + 1
        return f"✅ Written {lines} lines to {path}"
    except WorkspacePathError as e:
        return f"❌ {e}"
    except Exception as e:
        return f"❌ Failed to write {path}: {e}"


@tool
def file_edit(path: str, old_text: str, new_text: str) -> str:
    """Replace old_text with new_text in a file. Like search-and-replace. Use this for precise edits."""
    try:
        resolved = _safe_workspace_path(path)
        if not os.path.exists(resolved):
            return f"❌ File not found: {path}"

        with open(resolved, 'r') as f:
            content = f.read()

        if old_text not in content:
            # Show nearby content to help the agent find the right text
            return f"❌ Text not found in {path}. File has {len(content)} chars. First 200: {content[:200]}"

        count = content.count(old_text)
        new_content = content.replace(old_text, new_text, 1)  # Replace first occurrence only

        with open(resolved, 'w') as f:
            f.write(new_content)

        if count > 1:
            return f"✅ Replaced 1 of {count} occurrence(s) in {path} (first match only)"
        return f"✅ Replaced 1 occurrence in {path}"
    except WorkspacePathError as e:
        return f"❌ {e}"
    except Exception as e:
        return f"❌ Edit failed: {e}"


@tool
def git_status() -> str:
    """Show git status in the workspace — lists modified, added, and untracked files."""
    try:
        import subprocess
        root = _ensure_workspace()
        result = subprocess.run(
            ['git', 'status', '--short'],
            cwd=root, capture_output=True, text=True, timeout=10
        )
        if result.returncode != 0:
            if "not a git repository" in result.stderr:
                # Initialize git
                subprocess.run(['git', 'init'], cwd=root, capture_output=True)
                return "📂 Initialized new git repository. No changes yet."
            return f"❌ git error: {result.stderr}"

        output = result.stdout.strip()
        if not output:
            return "✅ Working tree clean — no uncommitted changes."
        return f"📂 Changes:\n```\n{output}\n```"
    except Exception as e:
        return f"❌ git status failed: {e}"


@tool
def git_diff() -> str:
    """Show diff of uncommitted changes in the workspace."""
    try:
        import subprocess
        root = _ensure_workspace()
        result = subprocess.run(
            ['git', 'diff', '--stat'],
            cwd=root, capture_output=True, text=True, timeout=10
        )
        if not result.stdout.strip():
            # Also check staged changes
            result = subprocess.run(
                ['git', 'diff', '--cached', '--stat'],
                cwd=root, capture_output=True, text=True, timeout=10
            )
        output = result.stdout.strip()
        if not output:
            return "No changes to show."

        # Also get the actual diff (truncated)
        full_diff = subprocess.run(
            ['git', 'diff'],
            cwd=root, capture_output=True, text=True, timeout=10
        )
        diff_text = full_diff.stdout[:2000]
        if len(full_diff.stdout) > 2000:
            diff_text += "\n... (truncated)"

        return f"📊 Diff summary:\n{output}\n\n```diff\n{diff_text}\n```"
    except Exception as e:
        return f"❌ git diff failed: {e}"


@tool
def git_commit(message: str) -> str:
    """Stage all changes and commit with the given message."""
    try:
        import subprocess
        root = _ensure_workspace()
        # Stage all
        subprocess.run(
            ['git', 'add', '-A'],
            cwd=root, capture_output=True, timeout=10
        )
        # Commit
        result = subprocess.run(
            ['git', 'commit', '-m', message],
            cwd=root, capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            return f"✅ Committed: {message}\n{result.stdout.strip()}"
        elif "nothing to commit" in result.stdout:
            return "ℹ️ Nothing to commit — working tree clean."
        else:
            return f"❌ Commit failed: {result.stderr}"
    except Exception as e:
        return f"❌ git commit failed: {e}"


@tool
def run_tests(test_command: str | None = None) -> str:
    """Run tests in the workspace. Auto-detects project type (pytest/mvn/npm) if no command given.
    Use this after file_write or file_edit to verify changes don't break anything."""
    try:
        import subprocess
        root = _ensure_workspace()

        if test_command:
            cmd = test_command
        else:
            # Auto-detect project type
            if os.path.exists(os.path.join(root, "pytest.ini")) or \
               os.path.exists(os.path.join(root, "pyproject.toml")) or \
               os.path.exists(os.path.join(root, "setup.py")):
                cmd = "python -m pytest --tb=short -q"
            elif os.path.exists(os.path.join(root, "pom.xml")):
                cmd = "mvn test -q"
            elif os.path.exists(os.path.join(root, "package.json")):
                cmd = "npm test"
            elif os.path.exists(os.path.join(root, "go.mod")):
                cmd = "go test ./..."
            else:
                return "⚠️ No test framework detected. Specify test_command manually."

        result = subprocess.run(
            cmd, shell=True,
            cwd=root, capture_output=True, text=True, timeout=60
        )
        output = result.stdout[-1500:] if len(result.stdout) > 1500 else result.stdout
        stderr = result.stderr[-500:] if len(result.stderr) > 500 else result.stderr

        if result.returncode == 0:
            return f"✅ Tests passed!\n```\n{output.strip()}\n```"
        else:
            return f"❌ Tests failed (exit code {result.returncode}):\n```\n{output.strip()}\n{stderr.strip()}\n```"
    except subprocess.TimeoutExpired:
        return "⏰ Tests timed out (60s limit)"
    except Exception as e:
        return f"❌ Test run failed: {e}"
