"""Tests for the shared FastAPI authentication dependency."""

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import jwt
import pytest
from fastapi import HTTPException
from starlette.responses import Response

from app.auth.middleware import (
    AuthContext,
    cache_verified_auth_context,
    require_auth,
    tenant_context,
)
from app.middleware.jwt_auth import JwtAuthMiddleware, verify_access_token


class FakeRequest:
    """Mock FastAPI Request."""
    def __init__(self, auth_header: str | None = None):
        self.headers = {}
        self.state = SimpleNamespace()
        self.url = SimpleNamespace(path="/v1/test")
        if auth_header:
            self.headers["Authorization"] = auth_header


@pytest.fixture
def mock_verifier():
    with patch(
        "app.middleware.jwt_auth.verify_access_token",
        new_callable=AsyncMock,
    ) as verifier:
        yield verifier


@pytest.mark.asyncio
async def test_require_auth_valid_token(mock_verifier):
    """Valid JWT should return AuthContext with correct claims."""
    mock_verifier.return_value = {
        "sub": "alice",
        "tenant_id": "tenant-1",
        "permissions": ["USER"],
    }

    request = FakeRequest("Bearer valid-token-here")
    result = await require_auth(request)

    assert isinstance(result, AuthContext)
    assert result.user_id == "alice"
    assert result.tenant_id == "tenant-1"
    assert result.permissions == ["USER"]
    assert tenant_context.get() == "tenant-1"
    assert request.state.auth_context == result
    mock_verifier.assert_awaited_once()


@pytest.mark.asyncio
async def test_require_auth_reuses_middleware_identity_without_reverification(mock_verifier):
    request = FakeRequest("Bearer already-verified")
    cache_verified_auth_context(
        request,
        AuthContext(
            user_id="alice",
            tenant_id="tenant-1",
            permissions=["USER"],
        ),
    )

    result = await require_auth(request)

    assert result is request.state.auth_context
    mock_verifier.assert_not_awaited()


@pytest.mark.asyncio
async def test_require_auth_does_not_trust_unmarked_request_state(mock_verifier):
    mock_verifier.return_value = {
        "sub": "verified-user",
        "tenant_id": "verified-tenant",
        "permissions": ["USER"],
    }
    request = FakeRequest("Bearer valid-token")
    request.state.auth_context = AuthContext(
        user_id="injected-user",
        tenant_id="injected-tenant",
        permissions=["*"],
    )

    result = await require_auth(request)

    assert result == AuthContext("verified-user", "verified-tenant", ["USER"])
    mock_verifier.assert_awaited_once()


@pytest.mark.asyncio
async def test_v1_middleware_and_dependency_verify_token_once(mock_verifier):
    mock_verifier.return_value = {
        "sub": "alice",
        "tenant_id": "tenant-1",
        "permissions": ["USER"],
    }
    middleware = JwtAuthMiddleware(
        app=object(),
        jwt_secret="local-dev-secret-at-least-32-bytes",
        protected_prefixes=["/v1/"],
        auth_service_url="http://auth",
    )
    request = FakeRequest("Bearer valid-token")
    observed = None

    async def call_next(active_request):
        nonlocal observed
        observed = await require_auth(active_request)
        return Response(status_code=200)

    response = await middleware.dispatch(request, call_next)

    assert response.status_code == 200
    assert observed == AuthContext("alice", "tenant-1", ["USER"])
    mock_verifier.assert_awaited_once()


@pytest.mark.asyncio
async def test_require_auth_missing_header():
    """Missing Authorization header should raise 401."""
    request = FakeRequest()
    with pytest.raises(HTTPException) as exc_info:
        await require_auth(request)
    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_require_auth_invalid_bearer():
    """Non-Bearer auth should raise 401."""
    request = FakeRequest("Basic dXNlcjpwYXNz")
    with pytest.raises(HTTPException) as exc_info:
        await require_auth(request)
    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_require_auth_expired_token(mock_verifier):
    """Expired JWT should raise 401."""
    mock_verifier.return_value = None

    request = FakeRequest("Bearer expired-token")
    with pytest.raises(HTTPException) as exc_info:
        await require_auth(request)
    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_require_auth_invalid_signature(mock_verifier):
    """Invalid signature should raise 401."""
    mock_verifier.return_value = None

    request = FakeRequest("Bearer bad-sig-token")
    with pytest.raises(HTTPException) as exc_info:
        await require_auth(request)
    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_tenant_context_propagation(mock_verifier):
    """tenant_context should be set after successful auth."""
    mock_verifier.return_value = {
        "sub": "bob",
        "tenant_id": "tenant-42",
        "permissions": ["USER", "ADMIN"],
    }

    request = FakeRequest("Bearer valid-token")
    await require_auth(request)

    # Verify context var is set
    assert tenant_context.get() == "tenant-42"


@pytest.mark.asyncio
async def test_legacy_hmac_requires_explicit_enablement():
    now = int(time.time())
    token = jwt.encode(
        {
            "sub": "local-user",
            "tenant_id": "local-tenant",
            "iss": "mcp-agent-local",
            "aud": "agent-server",
            "iat": now,
            "exp": now + 300,
            "jti": "local-token",
        },
        "local-dev-secret-at-least-32-bytes",
        algorithm="HS256",
    )

    assert await verify_access_token(
        token,
        auth_url="http://auth",
        jwt_secret="local-dev-secret-at-least-32-bytes",
    ) is None
    assert await verify_access_token(
        token,
        auth_url="http://auth",
        jwt_secret="local-dev-secret-at-least-32-bytes",
        allow_legacy_hmac=True,
    ) is not None


@pytest.mark.asyncio
async def test_legacy_hmac_rejects_missing_required_claims():
    token = jwt.encode(
        {"sub": "local-user", "tenant_id": "local-tenant"},
        "local-dev-secret-at-least-32-bytes",
        algorithm="HS256",
    )

    assert await verify_access_token(
        token,
        auth_url="http://auth",
        jwt_secret="local-dev-secret-at-least-32-bytes",
        allow_legacy_hmac=True,
    ) is None
