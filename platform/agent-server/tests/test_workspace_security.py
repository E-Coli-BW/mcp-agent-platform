"""Regression tests for review findings C1 + C2.

C1 — Process-Global Mutable Workspace: each tenant must see their own workspace
     even when other tenants concurrently call ``set_workspace_root`` (in
     multi-tenant mode). In legacy single-tenant mode the old shared-base
     behavior is preserved so existing tests still pass.

C2 — Path Traversal / Arbitrary File Access: ``validate_path`` must refuse:
       * absolute paths
       * ``..``-based escapes
       * symlinks pointing outside the workspace

These are the security contracts established by ``app.tools.workspace_resolver``.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.auth.middleware import tenant_context
from app.tools.workspace_resolver import (
    WorkspacePathError,
    WorkspaceResolver,
    _sanitize_tenant_id,
    validate_path,
)


# ── C2: validate_path ─────────────────────────────────────────────


class TestValidatePath:
    def test_accepts_relative_path_within_workspace(self, tmp_path):
        target = tmp_path / "subdir" / "file.txt"
        target.parent.mkdir()
        target.write_text("ok")
        assert validate_path("subdir/file.txt", str(tmp_path)) == str(target.resolve())

    def test_rejects_absolute_path(self, tmp_path):
        with pytest.raises(WorkspacePathError, match="Absolute paths"):
            validate_path("/etc/passwd", str(tmp_path))

    def test_rejects_dotdot_escape(self, tmp_path):
        with pytest.raises(WorkspacePathError, match="outside workspace"):
            validate_path("../../etc/passwd", str(tmp_path))

    def test_rejects_dotdot_with_subdir(self, tmp_path):
        (tmp_path / "foo").mkdir()
        with pytest.raises(WorkspacePathError, match="outside workspace"):
            validate_path("foo/../../outside", str(tmp_path))

    def test_rejects_empty_path(self, tmp_path):
        with pytest.raises(WorkspacePathError, match="Empty"):
            validate_path("", str(tmp_path))

    def test_rejects_symlink_pointing_outside_workspace(self, tmp_path):
        outside = tmp_path.parent / "outside_target"
        outside.mkdir(exist_ok=True)
        (outside / "secret.txt").write_text("attacker data")

        link = tmp_path / "evil_link"
        link.symlink_to(outside, target_is_directory=True)

        # Even though "evil_link/secret.txt" starts with the workspace name,
        # the symlink resolves outside — must be rejected.
        with pytest.raises(WorkspacePathError, match="symlink"):
            validate_path("evil_link/secret.txt", str(tmp_path))

    def test_accepts_symlink_pointing_inside_workspace(self, tmp_path):
        # Symlinks that stay inside the workspace are fine.
        (tmp_path / "real").mkdir()
        (tmp_path / "real" / "f.txt").write_text("data")
        (tmp_path / "link").symlink_to(tmp_path / "real", target_is_directory=True)
        resolved = validate_path("link/f.txt", str(tmp_path))
        assert "f.txt" in resolved
        # The resolved path must be inside the workspace
        assert Path(resolved).resolve().is_relative_to(tmp_path.resolve())

    def test_rejects_prefix_lookalike_path(self, tmp_path):
        # Old startswith() check would have accepted a sibling directory whose
        # name shares the workspace's prefix. is_relative_to() rejects it.
        sibling = tmp_path.parent / (tmp_path.name + "_evil")
        sibling.mkdir()
        (sibling / "data.txt").write_text("attacker")
        # Build a relative path that resolves to the sibling
        rel = os.path.relpath(sibling / "data.txt", tmp_path)
        with pytest.raises(WorkspacePathError):
            validate_path(rel, str(tmp_path))


# ── C1: per-tenant resolver isolation ─────────────────────────────


class TestWorkspaceResolverIsolation:
    def test_each_tenant_gets_distinct_directory(self, tmp_path):
        resolver = WorkspaceResolver(base=str(tmp_path), multi_tenant=True)
        ws_a = resolver.for_tenant("tenant-a")
        ws_b = resolver.for_tenant("tenant-b")
        assert ws_a != ws_b
        assert Path(ws_a).resolve().is_relative_to(tmp_path.resolve())
        assert Path(ws_b).resolve().is_relative_to(tmp_path.resolve())

    def test_set_for_tenant_does_not_leak_to_other_tenants(self, tmp_path):
        resolver = WorkspaceResolver(base=str(tmp_path), multi_tenant=True)
        attacker_dir = tmp_path / "attacker"
        attacker_dir.mkdir()

        resolver.set_for_tenant("attacker", str(attacker_dir))

        # The victim tenant must NOT see the attacker's pinned directory.
        victim_ws = resolver.for_tenant("victim")
        assert victim_ws != str(attacker_dir.resolve())
        assert "attacker" not in victim_ws

    def test_single_tenant_mode_shares_base(self, tmp_path):
        # Back-compat: legacy callers without auth share one workspace.
        resolver = WorkspaceResolver(base=str(tmp_path), multi_tenant=False)
        assert resolver.for_tenant("anyone") == str(tmp_path)
        assert resolver.for_tenant(None) == str(tmp_path)

    def test_unsafe_tenant_ids_are_sanitized(self):
        # Tenant ids from JWTs are theoretically attacker-controlled; we must not
        # let them break out of the base directory via path components.
        assert _sanitize_tenant_id("../../etc") == "etc"
        assert _sanitize_tenant_id("a/b\\c") == "a_b_c"
        assert _sanitize_tenant_id("") == "default"
        assert _sanitize_tenant_id(None) == "default"
        assert _sanitize_tenant_id("..") == "default"

    def test_resolver_creates_workspace_lazily(self, tmp_path):
        resolver = WorkspaceResolver(base=str(tmp_path), multi_tenant=True)
        ws = resolver.for_tenant("fresh-tenant")
        assert os.path.isdir(ws)


class TestTenantContextPropagation:
    def test_get_workspace_root_uses_context_var(self, tmp_path, monkeypatch):
        # Switch the module-level resolver to multi-tenant mode pointing at tmp_path.
        from app.tools import agent_mode, workspace_resolver

        new_resolver = workspace_resolver.WorkspaceResolver(base=str(tmp_path), multi_tenant=True)
        monkeypatch.setattr(workspace_resolver, "_resolver", new_resolver)

        token = tenant_context.set("alpha")
        try:
            ws_alpha = agent_mode.get_workspace_root()
        finally:
            tenant_context.reset(token)

        token = tenant_context.set("beta")
        try:
            ws_beta = agent_mode.get_workspace_root()
        finally:
            tenant_context.reset(token)

        assert ws_alpha != ws_beta
        assert "alpha" in ws_alpha
        assert "beta" in ws_beta
