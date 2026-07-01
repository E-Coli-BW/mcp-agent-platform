"""Tests for workspace API endpoints."""

import pytest
from httpx import AsyncClient, ASGITransport

from app.main import app
from app.tools.agent_mode import set_workspace_root


@pytest.fixture(autouse=True)
def workspace(tmp_path):
    set_workspace_root(str(tmp_path))
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("print('hello')\n")
    (tmp_path / "README.md").write_text("# Project\n")
    yield tmp_path


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


class TestWorkspaceAPI:
    async def test_get_current_workspace(self, client, workspace):
        resp = await client.get("/api/workspace/current")
        assert resp.status_code == 200
        data = resp.json()
        assert data["exists"] is True
        assert str(workspace) in data["path"]

    async def test_open_workspace(self, client, tmp_path):
        new_ws = tmp_path / "new_project"
        new_ws.mkdir()
        resp = await client.post("/api/workspace/open", json={"path": str(new_ws)})
        assert resp.status_code == 200
        assert str(new_ws) in resp.json()["path"]

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


class TestHealthEndpoint:
    async def test_health(self, client):
        resp = await client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
