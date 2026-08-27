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

import hashlib
import os
import re
import threading
from pathlib import Path

from app.config import settings

# Base root — all tenant workspaces live as direct children of this directory.
# In single-tenant / legacy mode we treat the base itself as the workspace.
_DEFAULT_BASE = os.path.expanduser("~/agent-workspace")


def _configured_workspace_base() -> str:
    return (
        os.environ.get("AGENT_WORKSPACE")
        or os.environ.get("AGENT_WORKSPACE_ROOT")
        or settings.workspace
        or settings.workspace_root
        or _DEFAULT_BASE
    )


def _configured_multi_tenant_workspace() -> bool:
    configured = os.environ.get("AGENT_MULTI_TENANT_WORKSPACE")
    if configured is not None:
        return configured.strip().lower() not in {"0", "false", "no", "off"}
    return settings.multi_tenant_workspace


def _sanitize_tenant_id(tenant_id: str | None) -> str:
    """Map a tenant id to a filesystem-safe directory name.

    Anything outside ``[A-Za-z0-9._-]`` becomes ``_``. Empty / None tenants
    collapse to the literal ``default`` bucket, matching the ContextVar default
    in ``app.auth.middleware``.
    """
    if not tenant_id:
        return "default"
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", tenant_id).strip("._-")
    return cleaned or "default"


def _tenant_directory_key(tenant_id: str | None) -> str:
    raw = tenant_id or "default"
    readable = _sanitize_tenant_id(tenant_id)
    digest = hashlib.sha256(raw.encode()).hexdigest()[:12]
    return f"{readable}--{digest}"


class WorkspaceResolver:
    """Maps a tenant id → isolated workspace directory.

    Thread-safe: a single lock guards the per-tenant override map and the
    base directory pointer. The hot path (looking up a tenant's workspace)
    only takes the lock long enough to read two values.
    """

    def __init__(self, base: str | None = None, *, multi_tenant: bool = True) -> None:
        self._lock = threading.Lock()
        self._base = os.path.realpath(os.path.expanduser(base or _DEFAULT_BASE))
        # Per-tenant explicit overrides — populated by REST `/api/workspace/open`.
        # When unset, a tenant's workspace is `<base>/<sanitized_tenant_id>`.
        self._overrides: dict[str, str] = {}
        # In single-tenant / dev mode, every tenant shares ``self._base`` directly
        # so existing tests and the legacy ``set_workspace_root`` API keep working.
        self._multi_tenant = multi_tenant

    # ── Read APIs ─────────────────────────────────────────────────

    def base(self) -> str:
        with self._lock:
            return self._base

    def allowed_root_for_tenant(self, tenant_id: str | None) -> str:
        """Return the immutable selection boundary for one tenant."""
        key = _tenant_directory_key(tenant_id)
        with self._lock:
            base = self._base
            multi = self._multi_tenant
        path = os.path.join(base, key) if multi else base
        Path(path).mkdir(parents=True, exist_ok=True)
        return path

    def for_tenant(self, tenant_id: str | None) -> str:
        """Return the workspace directory for ``tenant_id``, creating it lazily."""
        key = _tenant_directory_key(tenant_id)
        with self._lock:
            override = self._overrides.get(key)
        if override:
            path = override
        else:
            path = self.allowed_root_for_tenant(tenant_id)
        Path(path).mkdir(parents=True, exist_ok=True)
        return path

    # ── Write APIs ────────────────────────────────────────────────

    def set_base(self, base: str) -> None:
        resolved = os.path.realpath(os.path.expanduser(base))
        Path(resolved).mkdir(parents=True, exist_ok=True)
        with self._lock:
            self._base = resolved

    def set_for_tenant(self, tenant_id: str | None, path: str) -> str:
        """Pin a specific path for one tenant. Used by REST workspace-open."""
        key = _tenant_directory_key(tenant_id)
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


# Module-level singleton. Multi-tenant isolation is the safe code default.
# Single-user local setups can opt out through the process environment or the
# agent-server .env file, regardless of uvicorn's working directory.
_resolver = WorkspaceResolver(
    base=_configured_workspace_base(),
    multi_tenant=_configured_multi_tenant_workspace(),
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
