"""Tests for workspace API endpoints."""

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from app.auth.middleware import AuthContext, require_auth
from app.main import app
from app.tools.workspace_resolver import WorkspaceResolver, get_resolver


@pytest.fixture(autouse=True)
def workspace(tmp_path, monkeypatch):
    from app.tools import workspace_resolver

    resolver = WorkspaceResolver(base=str(tmp_path / "tenants"), multi_tenant=True)
    monkeypatch.setattr(workspace_resolver, "_resolver", resolver)
    root = Path(resolver.for_tenant("default"))
    (root / "src").mkdir()
    (root / "src" / "main.py").write_text("print('hello')\n")
    (root / "README.md").write_text("# Project\n")
    yield root


@pytest.fixture
async def client():
    app.dependency_overrides[require_auth] = lambda: AuthContext(
        user_id="workspace-test",
        tenant_id="default",
        permissions=[],
    )
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c
    finally:
        app.dependency_overrides.pop(require_auth, None)


class TestWorkspaceAPI:
    async def test_get_current_workspace(self, client, workspace):
        resp = await client.get("/api/workspace/current")
        assert resp.status_code == 200
        data = resp.json()
        assert data["exists"] is True
        assert str(workspace) in data["path"]

    async def test_open_workspace(self, client, workspace):
        new_ws = workspace / "new_project"
        new_ws.mkdir()
        resp = await client.post("/api/workspace/open", json={"path": str(new_ws)})
        assert resp.status_code == 200
        assert str(new_ws) in resp.json()["path"]

    async def test_open_workspace_does_not_create_missing_directory(self, client, workspace):
        missing = workspace / "mistyped-project"

        resp = await client.post("/api/workspace/open", json={"path": str(missing)})

        assert resp.status_code == 400
        assert not missing.exists()

    async def test_single_tenant_local_mode_can_open_another_project(
        self,
        client,
        workspace,
    ):
        resolver = get_resolver()
        resolver.set_multi_tenant(False)
        other_project = workspace.parent / "other-project"
        other_project.mkdir()

        resp = await client.post(
            "/api/workspace/open",
            json={"path": str(other_project)},
        )

        assert resp.status_code == 200
        assert resp.json()["path"] == str(other_project.resolve())

    async def test_browse_workspace_returns_canonical_navigation_paths(self, client, workspace):
        root = workspace / "browse-root"
        child = root / "project-a"
        child.mkdir(parents=True)

        resp = await client.get("/api/workspace/browse", params={"path": str(root)})

        assert resp.status_code == 200
        body = resp.json()
        assert body["path"] == str(root.resolve())
        assert body["parent"] == str(workspace.resolve())
        assert body["dirs"] == [
            {"name": "project-a", "path": str(child.resolve())}
        ]

    async def test_list_files(self, client):
        resp = await client.get("/api/workspace/files")
        assert resp.status_code == 200
        data = resp.json()
        assert "tree" in data
        names = [n["name"] for n in data["tree"]]
        assert "src" in names or any("src" in str(n) for n in data["tree"])

    async def test_read_file(self, client):
        resp = await client.get("/api/workspace/file", params={"path": "README.md"})
        assert resp.status_code == 200
        data = resp.json()
        assert "# Project" in data["content"]
        assert data["language"] == "markdown"

    async def test_read_file_not_found(self, client):
        resp = await client.get("/api/workspace/file", params={"path": "nope.py"})
        assert resp.status_code == 404

    async def test_read_file_outside_workspace(self, client):
        resp = await client.get("/api/workspace/file", params={"path": "../../etc/passwd"})
        assert resp.status_code == 403

    async def test_tenant_cannot_open_another_tenants_workspace(self, client):
        victim_workspace = Path(get_resolver().for_tenant("victim"))
        app.dependency_overrides[require_auth] = lambda: AuthContext(
            user_id="attacker",
            tenant_id="attacker",
            permissions=[],
        )

        resp = await client.post(
            "/api/workspace/open",
            json={"path": str(victim_workspace)},
        )

        assert resp.status_code == 403

    async def test_browse_defaults_to_authenticated_tenant_root(self, client):
        resp = await client.get("/api/workspace/browse")

        assert resp.status_code == 200
        assert resp.json()["path"] == str(
            Path(get_resolver().allowed_root_for_tenant("default")).resolve()
        )


class TestHealthEndpoint:
    async def test_health(self, client):
        resp = await client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"


async def test_workspace_api_requires_authentication():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/workspace/current")

    assert response.status_code == 401
