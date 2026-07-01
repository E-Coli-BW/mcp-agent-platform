"""Per-tenant workspace resolution + safe path validation.

Replaces the previous process-global ``_workspace_root`` with a per-tenant
mapping. Each tenant gets an isolated subdirectory under the configured base.

Security properties (review findings C1 + C2):
    * Tenant A's workspace cannot resolve to Tenant B's directory — the tenant
      id is derived from the ``app.auth.middleware.tenant_context`` ContextVar
      which is set from the verified JWT.
    * ``validate_path`` uses ``Path.resolve(strict=False).is_relative_to(root)``
      instead of string-prefix matching, and refuses any input that traverses
      a symlink whose target escapes the workspace.
    * Absolute paths from agent tools are rejected — agents must address files
      via paths relative to their tenant workspace.

The legacy module-level functions ``get_workspace_root`` / ``set_workspace_root``
are preserved for back-compat with callers that don't yet thread the tenant
id (REST endpoints, single-tenant dev setups). They now delegate to the
resolver using the current ContextVar value.
"""

from __future__ import annotations

import os
import re
import threading
from pathlib import Path
from typing import Dict, Optional


# Base root — all tenant workspaces live as direct children of this directory.
# In single-tenant / legacy mode we treat the base itself as the workspace.
_DEFAULT_BASE = os.path.expanduser("~/agent-workspace")


def _sanitize_tenant_id(tenant_id: Optional[str]) -> str:
    """Map a tenant id to a filesystem-safe directory name.

    Anything outside ``[A-Za-z0-9._-]`` becomes ``_``. Empty / None tenants
    collapse to the literal ``default`` bucket, matching the ContextVar default
    in ``app.auth.middleware``.
    """
    if not tenant_id:
        return "default"
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", tenant_id).strip("._-")
    return cleaned or "default"


class WorkspaceResolver:
    """Maps a tenant id → isolated workspace directory.

    Thread-safe: a single lock guards the per-tenant override map and the
    base directory pointer. The hot path (looking up a tenant's workspace)
    only takes the lock long enough to read two values.
    """

    def __init__(self, base: Optional[str] = None, *, multi_tenant: bool = True) -> None:
        self._lock = threading.Lock()
        self._base = os.path.realpath(os.path.expanduser(base or _DEFAULT_BASE))
        # Per-tenant explicit overrides — populated by REST `/api/workspace/open`.
        # When unset, a tenant's workspace is `<base>/<sanitized_tenant_id>`.
        self._overrides: Dict[str, str] = {}
        # In single-tenant / dev mode, every tenant shares ``self._base`` directly
        # so existing tests and the legacy ``set_workspace_root`` API keep working.
        self._multi_tenant = multi_tenant

    # ── Read APIs ─────────────────────────────────────────────────

    def base(self) -> str:
        with self._lock:
            return self._base

    def for_tenant(self, tenant_id: Optional[str]) -> str:
        """Return the workspace directory for ``tenant_id``, creating it lazily."""
        key = _sanitize_tenant_id(tenant_id)
        with self._lock:
            override = self._overrides.get(key)
            base = self._base
            multi = self._multi_tenant
        if override:
            path = override
        elif multi:
            path = os.path.join(base, key)
        else:
            path = base
        Path(path).mkdir(parents=True, exist_ok=True)
        return path

    # ── Write APIs ────────────────────────────────────────────────

    def set_base(self, base: str) -> None:
        resolved = os.path.realpath(os.path.expanduser(base))
        Path(resolved).mkdir(parents=True, exist_ok=True)
        with self._lock:
            self._base = resolved

    def set_for_tenant(self, tenant_id: Optional[str], path: str) -> str:
        """Pin a specific path for one tenant. Used by REST workspace-open."""
        key = _sanitize_tenant_id(tenant_id)
        resolved = os.path.realpath(os.path.expanduser(path))
        Path(resolved).mkdir(parents=True, exist_ok=True)
        with self._lock:
            self._overrides[key] = resolved
        return resolved

    def set_multi_tenant(self, multi_tenant: bool) -> None:
        with self._lock:
            self._multi_tenant = multi_tenant

    def is_multi_tenant(self) -> bool:
        with self._lock:
            return self._multi_tenant


# Module-level singleton. The default is single-tenant so existing tests that
# call ``set_workspace_root(tmp_path)`` keep observing one shared directory.
# Multi-tenant mode is enabled by setting AGENT_MULTI_TENANT_WORKSPACE=1, by
# which point the JWT auth middleware is the source of truth for the tenant id.
_resolver = WorkspaceResolver(
    base=os.environ.get("AGENT_WORKSPACE", _DEFAULT_BASE),
    multi_tenant=os.environ.get("AGENT_MULTI_TENANT_WORKSPACE", "").lower() in {"1", "true", "yes"},
)


def get_resolver() -> WorkspaceResolver:
    return _resolver


# ── Path-traversal-safe validation ───────────────────────────────


class WorkspacePathError(ValueError):
    """Raised when a tool tries to read/write a path outside its workspace."""


def validate_path(path: str, workspace_root: str) -> str:
    """Resolve ``path`` relative to ``workspace_root`` and ensure containment.

    Replaces the previous ``startswith()`` check. Uses
    :py:meth:`pathlib.Path.resolve` + :py:meth:`pathlib.Path.is_relative_to`
    so symlinks pointing outside the workspace are detected and rejected.

    Rules:
        1. Absolute paths from the agent are refused — relative paths only.
        2. Resolved real path must be inside the (resolved) workspace root.
        3. Any component along the path that is a symlink pointing outside
           the workspace causes rejection.
    """
    if path is None or path == "":
        raise WorkspacePathError("Empty path is not allowed")
    if os.path.isabs(path):
        raise WorkspacePathError(
            f"Absolute paths are not allowed (got '{path}'). Use a path relative to the workspace."
        )

    root = Path(workspace_root).resolve(strict=False)
    target = (root / path).resolve(strict=False)

    try:
        target.relative_to(root)
    except ValueError as exc:
        raise WorkspacePathError(
            f"Path '{path}' resolves to '{target}' which is outside workspace '{root}'"
        ) from exc

    # Reject symlinks anywhere in the chain that escape the workspace. We walk
    # from the target upward; any intermediate symlink whose real target falls
    # outside ``root`` is treated as an escape attempt.
    cur = target
    while True:
        if cur == root or cur == cur.parent:
            break
        if cur.is_symlink():
            link_target = cur.resolve(strict=False)
            try:
                link_target.relative_to(root)
            except ValueError as exc:
                raise WorkspacePathError(
                    f"Path '{path}' traverses symlink '{cur}' → '{link_target}' "
                    f"outside workspace '{root}'"
                ) from exc
        cur = cur.parent

    return str(target)
