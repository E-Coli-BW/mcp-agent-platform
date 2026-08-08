from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import time

import pytest
from fastapi import Request
from fastapi.testclient import TestClient

from app.api import runtime_control
from app.auth.middleware import AuthContext, require_auth
from app.config import settings
from app.main import app
from app.runtime import get_run_recorder, reset_run_recorder_for_tests
from app.runtime.approval import ApprovalRegistry
from app.runtime.checkpoint import SQLiteCheckpointStore


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _auth_headers(tenant_id: str, user_id: str = "test-user") -> dict[str, str]:
    secret = settings.jwt_secret.encode()
    if len(secret) < 32:
        secret = secret.ljust(32, b"\x00")
    header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = _b64url(
        json.dumps(
            {
                "sub": user_id,
                "tenant_id": tenant_id,
                "exp": int(time.time()) + 300,
            }
        ).encode()
    )
    signing_input = f"{header}.{payload}".encode()
    signature = _b64url(hmac.new(secret, signing_input, hashlib.sha256).digest())
    return {"Authorization": f"Bearer {header}.{payload}.{signature}"}


@pytest.fixture
def runtime_api(tmp_path, monkeypatch):
    database = tmp_path / "runtime.sqlite3"
    registry = ApprovalRegistry(str(database))
    checkpoints = SQLiteCheckpointStore(database)
    monkeypatch.setattr(runtime_control, "get_approval_registry", lambda: registry)
    monkeypatch.setattr(runtime_control, "get_checkpoint_store", lambda: checkpoints)
    monkeypatch.setattr(
        "app.runtime.checkpoint.get_checkpoint_store",
        lambda: checkpoints,
    )

    async def no_jwks(_auth_url: str):
        return None

    monkeypatch.setattr("app.middleware.jwt_auth._fetch_jwks", no_jwks)

    async def auth_from_middleware(request: Request) -> AuthContext:
        return AuthContext(
            user_id=request.state.jwt_subject,
            tenant_id=request.state.tenant_id,
            permissions=[],
        )

    app.dependency_overrides[require_auth] = auth_from_middleware
    asyncio.run(reset_run_recorder_for_tests())
    try:
        yield TestClient(app), registry, checkpoints
    finally:
        app.dependency_overrides.pop(require_auth, None)
        asyncio.run(reset_run_recorder_for_tests())


def _start_run(run_id: str, tenant_id: str) -> None:
    asyncio.run(
        get_run_recorder().start_run(
            run_id=run_id,
            request_id=f"request-{run_id}",
            tenant_id=tenant_id,
            session_id=f"{tenant_id}:session-1",
            model="coding-agent",
        )
    )


def test_approval_decision_and_checkpoint_are_tenant_scoped(runtime_api):
    client, registry, checkpoints = runtime_api
    _start_run("run-1", "tenant-a")
    approval = asyncio.run(
        registry.create(
            run_id="run-1",
            tenant_id="tenant-a",
            tool_name="file_write",
            reason="write requires approval",
        )
    )
    checkpoints.save(
        run_id="run-1",
        step_seq=1,
        state="waiting_approval",
        tool_name="file_write",
        approval_id=approval.approval_id,
        payload={"arguments": {"path": "a.txt"}},
    )

    assert client.get(
        f"/v1/approvals/{approval.approval_id}",
        headers=_auth_headers("tenant-b"),
    ).status_code == 403
    assert client.post(
        f"/v1/approvals/{approval.approval_id}/decision",
        headers=_auth_headers("tenant-b"),
        json={"status": "approved"},
    ).status_code == 403

    response = client.post(
        f"/v1/approvals/{approval.approval_id}/decision",
        headers=_auth_headers("tenant-a", "approver-1"),
        json={"status": "approved"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "approved"
    assert response.json()["decided_by"] == "approver-1"
    assert checkpoints.get("run-1").state == "approved"
    assert client.get(
        "/v1/runs/run-1/checkpoint",
        headers=_auth_headers("tenant-b"),
    ).status_code == 403


def test_resume_executes_approved_checkpoint_once(runtime_api, monkeypatch):
    client, registry, checkpoints = runtime_api
    _start_run("run-2", "tenant-a")
    approval = asyncio.run(
        registry.create(
            run_id="run-2",
            tenant_id="tenant-a",
            tool_name="file_write",
            reason="write requires approval",
        )
    )
    asyncio.run(
        registry.decide(
            approval.approval_id,
            status="approved",
            decided_by="approver-1",
        )
    )
    checkpoints.save(
        run_id="run-2",
        step_seq=1,
        state="approved",
        tool_name="file_write",
        approval_id=approval.approval_id,
        payload={
            "arguments": {"path": "a.txt", "content": "hello"},
            "session_id": "tenant-a:session-1",
        },
    )
    calls = []

    class FakeTool:
        async def ainvoke(self, arguments):
            calls.append(arguments)
            return "written"

    monkeypatch.setattr(
        "app.registry.tool_registry.get_registered_tools",
        lambda: {"file_write": FakeTool()},
    )

    first = client.post(
        "/v1/runs/run-2/resume",
        headers=_auth_headers("tenant-a", "approver-1"),
    )
    second = client.post(
        "/v1/runs/run-2/resume",
        headers=_auth_headers("tenant-a", "approver-1"),
    )

    assert first.status_code == 200
    assert first.json()["accepted"] is True
    assert first.json()["checkpoint"]["state"] == "completed"
    assert second.status_code == 200
    assert second.json()["accepted"] is False
    assert second.json()["reason"] == "checkpoint is not approved"
    assert calls == [{"path": "a.txt", "content": "hello"}]
