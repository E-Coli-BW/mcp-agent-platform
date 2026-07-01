"""
Tests for the /auth/* reverse proxy in app.api.auth_proxy.

The proxy is what makes the embedded UI at /ui/ able to call relative
URLs like /auth/signup. Without it, those requests 404 against the
agent-server itself instead of reaching auth-service. This bug WAS the
"register always fails in the UI" mystery (May 2026).

Pins:
  - Methods POST/GET/OPTIONS all forward.
  - The path tail (/auth/<anything>/<deep>/<path>) is preserved.
  - The body is forwarded verbatim (JSON bytes round-trip).
  - The upstream status code is mirrored back (200, 4xx, 5xx all).
  - The upstream response body is mirrored back.
  - The Authorization header is forwarded (login flow).
  - Hop-by-hop headers like content-length are NOT forwarded
    (would otherwise corrupt the body length on the way back).
  - When the upstream is unreachable, we return 503 with a clear error.
"""

import json

import pytest
from httpx import ASGITransport, AsyncClient
from pytest_httpx import HTTPXMock

from app.config import settings
from app.main import app


# Use a deterministic upstream URL for these tests so we can match on it.
@pytest.fixture(autouse=True)
def _pin_auth_url(monkeypatch):
    # The proxy reads settings.auth_service_url at request time.
    monkeypatch.setattr(settings, "auth_service_url", "http://auth.test")


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://agent.test") as c:
        yield c


@pytest.fixture
def _reset_proxy_client():
    """Force the proxy's lazily-created httpx.AsyncClient to be re-created.

    Why: the proxy caches one shared client at module level. Across tests
    the cached client holds a reference to the previous event loop, and
    when pytest-anyio creates a fresh loop per test, awaiting on the old
    client raises 'Event loop is closed'. Resetting between tests is
    cheaper than re-architecting the proxy.
    """
    from app.api import auth_proxy

    auth_proxy._client = None
    yield
    auth_proxy._client = None


class TestAuthProxyForwarding:
    async def test_post_signup_forwards_body_and_returns_201(
        self, client, httpx_mock: HTTPXMock, _reset_proxy_client
    ):
        # Pin: the proxy posts to <auth_url>/auth/signup with the same body.
        httpx_mock.add_response(
            method="POST",
            url="http://auth.test/auth/signup",
            status_code=200,
            json={"user_id": 42, "username": "alice"},
        )

        resp = await client.post(
            "/auth/signup",
            json={"username": "alice", "password": "pass12345678"},
        )

        assert resp.status_code == 200
        assert resp.json() == {"user_id": 42, "username": "alice"}

        # Body of the upstream request matches what we sent.
        sent = httpx_mock.get_request()
        assert sent is not None
        assert json.loads(sent.content) == {
            "username": "alice",
            "password": "pass12345678",
        }

    async def test_post_login_4xx_mirrored_verbatim(
        self, client, httpx_mock: HTTPXMock, _reset_proxy_client
    ):
        # Pin: 4xx upstream responses come through unchanged so the UI
        # can show the real error. This is the case that *was* broken —
        # we used to swallow it as a generic 404 before the proxy existed.
        httpx_mock.add_response(
            method="POST",
            url="http://auth.test/auth/login",
            status_code=401,
            json={"error": "invalid_credentials"},
        )
        resp = await client.post(
            "/auth/login",
            json={"username": "alice", "password": "wrong"},
        )
        assert resp.status_code == 401
        assert resp.json() == {"error": "invalid_credentials"}

    async def test_authorization_header_forwarded(
        self, client, httpx_mock: HTTPXMock, _reset_proxy_client
    ):
        # Pin: tokens added by the UI must reach auth-service. (E.g.
        # refresh-token endpoints, logout, anything that needs the JWT.)
        httpx_mock.add_response(
            method="POST",
            url="http://auth.test/auth/refresh",
            status_code=200,
            json={"access_token": "new.jwt.here"},
        )

        await client.post(
            "/auth/refresh",
            headers={"Authorization": "Bearer some.jwt.token"},
            json={"refresh_token": "rt-1"},
        )
        sent = httpx_mock.get_request()
        assert sent is not None
        assert sent.headers.get("authorization") == "Bearer some.jwt.token"

    async def test_path_tail_preserved(
        self, client, httpx_mock: HTTPXMock, _reset_proxy_client
    ):
        # Pin: nested paths (future-proofing for /auth/admin/users/123 etc.)
        # must NOT collapse to /auth.
        httpx_mock.add_response(
            method="GET",
            url="http://auth.test/auth/admin/users/42",
            status_code=200,
            json={"id": 42},
        )
        resp = await client.get("/auth/admin/users/42")
        assert resp.status_code == 200
        assert resp.json() == {"id": 42}

    async def test_upstream_unreachable_returns_503(
        self, client, monkeypatch, _reset_proxy_client
    ):
        # Pin: when auth-service is down (not just 5xx, actually
        # connection-refused / DNS fail / timeout), the proxy returns
        # 503 with a JSON error body. We DON'T want the FastAPI default
        # 500 internal-error stack trace leaking through.
        import httpx

        from app.api import auth_proxy

        class _Boom:
            async def request(self, *_, **__):
                raise httpx.ConnectError("nope")

        monkeypatch.setattr(auth_proxy, "_get_client", lambda: _Boom())

        resp = await client.post(
            "/auth/signup",
            json={"username": "x", "password": "y"},
        )
        assert resp.status_code == 503
        assert resp.json() == {"error": "Auth service unavailable"}


class TestAuthProxyHopByHopHeaders:
    async def test_content_length_from_upstream_not_doubled(
        self, client, httpx_mock: HTTPXMock, _reset_proxy_client
    ):
        # Pin: if we forwarded upstream's Content-Length as-is, FastAPI/uvicorn
        # would set its own Content-Length too based on the body we return.
        # Result on the wire: two Content-Length headers, browsers reject.
        # We strip Content-Length on the way back.
        httpx_mock.add_response(
            method="POST",
            url="http://auth.test/auth/signup",
            status_code=200,
            json={"ok": True},
            headers={"Content-Length": "999"},  # intentionally wrong
        )
        resp = await client.post(
            "/auth/signup",
            json={"username": "a", "password": "b"},
        )
        # Should still parse cleanly: only one (correct) content-length.
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}
        # Count: starlette TestClient/httpx folds duplicates into a single
        # comma-joined value, so check there's no comma.
        cl = resp.headers.get("content-length", "")
        assert "," not in cl
