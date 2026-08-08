from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import time

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.runtime import RunStatus, get_run_recorder, reset_run_recorder_for_tests


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _mint_hmac_token(*, tenant_id: str, sub: str = "test-user") -> str:
    secret = settings.jwt_secret.encode()
    if len(secret) < 32:
        secret = secret.ljust(32, b"\x00")

    header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = _b64url(
        json.dumps({
            "sub": sub,
            "tenant_id": tenant_id,
            "exp": int(time.time()) + 300,
        }).encode()
    )
    signing_input = f"{header}.{payload}".encode()
    sig = _b64url(hmac.new(secret, signing_input, hashlib.sha256).digest())
    return f"{header}.{payload}.{sig}"


def _auth_headers(tenant_id: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {_mint_hmac_token(tenant_id=tenant_id)}"}


@pytest.fixture(autouse=True)
def _reset_recorder():
    asyncio.run(reset_run_recorder_for_tests())
    yield
    asyncio.run(reset_run_recorder_for_tests())


@pytest.fixture
def client():
    from app.main import app

    return TestClient(app)


def test_get_run_events_returns_tenant_scoped_timeline(client):
    recorder = get_run_recorder()
    asyncio.run(
        recorder.start_run(
            run_id="run-1",
            request_id="req-1",
            tenant_id="acme",
            session_id="acme:s1",
            model="coding-agent",
        )
    )
    asyncio.run(recorder.record_event("run-1", "status_thinking"))
    asyncio.run(recorder.finish_run("run-1", RunStatus.COMPLETED))

    resp = client.get(
        "/v1/runs/run-1/events?after_seq=1",
        headers=_auth_headers("acme"),
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["run_id"] == "run-1"
    assert body["status"] == "completed"
    assert [event["event_type"] for event in body["events"]] == [
        "status_thinking",
        "run_completed",
    ]
    assert body["events"][0]["event_id"] == "run-1:2"


def test_get_run_events_rejects_cross_tenant_access(client):
    recorder = get_run_recorder()
    asyncio.run(
        recorder.start_run(
            run_id="run-1",
            request_id="req-1",
            tenant_id="victim",
            session_id="victim:s1",
            model="coding-agent",
        )
    )

    resp = client.get(
        "/v1/runs/run-1/events",
        headers=_auth_headers("attacker"),
    )

    assert resp.status_code == 403, resp.text


def test_get_run_events_requires_auth(client):
    resp = client.get("/v1/runs/run-1/events")

    assert resp.status_code == 401


def test_runtime_control_urls_are_not_registered_by_chat_router():
    from app.api.chat import router as chat_router
    from app.api.runtime_control import router as runtime_control_router

    runtime_paths = {route.path for route in runtime_control_router.routes}
    chat_paths = {route.path for route in chat_router.routes}
    expected_paths = {
        "/v1/runs/{run_id}/events",
        "/v1/runs/{run_id}/checkpoint",
        "/v1/runtime/recoverable",
        "/v1/runs/{run_id}/resume",
        "/v1/runs/{run_id}/cancel",
        "/v1/approvals/{approval_id}",
        "/v1/approvals/{approval_id}/decision",
        "/v1/sessions/{session_id}/children/{child_id}/cancel",
    }

    assert expected_paths <= runtime_paths
    assert chat_paths.isdisjoint(runtime_paths)
