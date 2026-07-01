"""Tests for the POST /v1/sessions/{id}/children/{id}/cancel endpoint.

These tests focus on the HTTP contract — authentication, tenant
isolation, and the 200 response shape. The actual cancellation
mechanics (does the child stop?) are covered in test_fleet_bus.py.

Run:
    cd platform/agent-server
    .venv/bin/python -m pytest tests/test_cancel_endpoint.py -q

Why we mint HMAC JWTs here (instead of dependency_overrides):
    The agent-server protects /v1/** via an ASGI-level middleware
    (JwtAuthMiddleware), which runs BEFORE FastAPI dependencies are
    resolved. dependency_overrides on require_auth therefore can't
    bypass it — the middleware returns 401 before the endpoint
    function (or its deps) ever execute. The cheapest reliable path
    is to mint a token in the same HMAC format the middleware will
    accept. _mint_hmac_token below mirrors _verify_hmac in jwt_auth.py.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import time

import pytest
from fastapi.testclient import TestClient

from app.agent import fleet_bus
from app.config import settings


# ── HMAC JWT mint helper ────────────────────────────────────────────────────
def _b64url(data: bytes) -> str:
    """URL-safe base64 with no padding — the format every JWT uses."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _mint_hmac_token(*, tenant_id: str, sub: str = "test-user") -> str:
    """Forge an HMAC-SHA256 JWT that JwtAuthMiddleware._verify_hmac will accept.

    Mirrors the production verifier exactly (same header, same secret
    padding to 32 bytes). Keeps tests in lockstep with the verifier —
    if anyone changes the secret-padding rule both sides update together.
    """
    secret = settings.jwt_secret.encode()
    if len(secret) < 32:
        secret = secret.ljust(32, b"\x00")

    header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = _b64url(
        json.dumps({
            "sub": sub,
            "tenant_id": tenant_id,
            "exp": int(time.time()) + 300,  # 5-minute window
        }).encode()
    )
    signing_input = f"{header}.{payload}".encode()
    sig = _b64url(hmac.new(secret, signing_input, hashlib.sha256).digest())
    return f"{header}.{payload}.{sig}"


@pytest.fixture(autouse=True)
def _reset_bus():
    """Always start each test with a clean bus."""
    fleet_bus._reset_for_tests()
    yield
    fleet_bus._reset_for_tests()


@pytest.fixture
def client():
    """Shared FastAPI test client. The auth token is supplied per-test
    via the Authorization header so each test can claim a different
    tenant identity."""
    from app.main import app
    return TestClient(app)


def _auth_headers(tenant_id: str) -> dict:
    return {"Authorization": f"Bearer {_mint_hmac_token(tenant_id=tenant_id)}"}


# ── HTTP contract tests ────────────────────────────────────────────────────
class TestCancelEndpoint:

    def test_should_acceptCancel_when_sessionRegistered(self, client):
        """Happy path: bus knows the session, cancel returns accepted=True."""
        # Register a session for tenant "acme". In production this happens
        # inside chat_completions when streaming starts.
        asyncio.run(fleet_bus.register_session("acme:my-session-1"))

        resp = client.post(
            "/v1/sessions/acme:my-session-1/children/child-abc/cancel",
            headers=_auth_headers("acme"),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["accepted"] is True
        assert body["session_id"] == "acme:my-session-1"
        assert body["child_session_id"] == "child-abc"

        # The bus must now report the child as cancelled.
        assert fleet_bus.is_cancelled(
            root_session_id="acme:my-session-1",
            child_session_id="child-abc",
        )

    def test_should_returnAccepted_when_sessionUnknown(self, client):
        """Unknown session = 200 with accepted=False, NOT a 404.

        Justification: the typical race is 'cancel arrives after the
        child has already finished and the session torn down'. That's
        not an error — the desired outcome (child has stopped) is
        already achieved. Returning 404 here would make UIs surface
        spurious errors on every snappy completion.
        """
        resp = client.post(
            "/v1/sessions/acme:ghost-session/children/child-xyz/cancel",
            headers=_auth_headers("acme"),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["accepted"] is False
        # The note must explain the situation so a UI can decide whether
        # to show 'cancelled' or 'already complete'.
        assert "completed" in body["note"].lower() or "not found" in body["note"].lower()

    def test_should_reject403_when_tenantMismatch(self, client):
        """Cross-tenant cancel = 403. Without this check a user in tenant
        A could cancel work in tenant B by guessing session ids — a
        denial-of-service vector. Pinned because regressions here are
        invisible from the happy path."""
        asyncio.run(fleet_bus.register_session("victim:secret-session"))

        # Authenticate as attacker, target victim's session.
        resp = client.post(
            "/v1/sessions/victim:secret-session/children/c1/cancel",
            headers=_auth_headers("attacker"),
        )
        assert resp.status_code == 403, resp.text
        # The victim's child must NOT have been flagged for cancellation.
        assert not fleet_bus.is_cancelled(
            root_session_id="victim:secret-session",
            child_session_id="c1",
        )

    def test_should_require_auth(self, client):
        """No auth header = 401, just like other protected endpoints."""
        resp = client.post(
            "/v1/sessions/acme:x/children/c/cancel"
        )
        assert resp.status_code == 401
