"""Tests for FastAPI auth middleware."""

import time
import pytest
from unittest.mock import patch, MagicMock
from fastapi import HTTPException

from app.auth.middleware import require_auth, AuthContext, tenant_context


class FakeRequest:
    """Mock FastAPI Request."""
    def __init__(self, auth_header: str | None = None):
        self.headers = {}
        if auth_header:
            self.headers["Authorization"] = auth_header


@pytest.fixture
def mock_jwks():
    """Mock PyJWKClient and jwt.decode."""
    with patch("app.auth.middleware._get_jwks_client") as mock_client, \
         patch("app.auth.middleware.jwt.decode") as mock_decode:
        yield mock_client, mock_decode


@pytest.mark.asyncio
async def test_require_auth_valid_token(mock_jwks):
    """Valid JWT should return AuthContext with correct claims."""
    mock_client, mock_decode = mock_jwks
    mock_signing_key = MagicMock()
    mock_signing_key.key = "fake-key"
    mock_client.return_value.get_signing_key_from_jwt.return_value = mock_signing_key
    mock_decode.return_value = {
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
async def test_require_auth_expired_token(mock_jwks):
    """Expired JWT should raise 401."""
    mock_client, mock_decode = mock_jwks
    mock_signing_key = MagicMock()
    mock_signing_key.key = "fake-key"
    mock_client.return_value.get_signing_key_from_jwt.return_value = mock_signing_key

    import jwt as pyjwt
    mock_decode.side_effect = pyjwt.ExpiredSignatureError("Token expired")

    request = FakeRequest("Bearer expired-token")
    with pytest.raises(HTTPException) as exc_info:
        await require_auth(request)
    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_require_auth_invalid_signature(mock_jwks):
    """Invalid signature should raise 401."""
    mock_client, mock_decode = mock_jwks
    mock_signing_key = MagicMock()
    mock_signing_key.key = "fake-key"
    mock_client.return_value.get_signing_key_from_jwt.return_value = mock_signing_key

    import jwt as pyjwt
    mock_decode.side_effect = pyjwt.InvalidSignatureError("Bad signature")

    request = FakeRequest("Bearer bad-sig-token")
    with pytest.raises(HTTPException) as exc_info:
        await require_auth(request)
    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_tenant_context_propagation(mock_jwks):
    """tenant_context should be set after successful auth."""
    mock_client, mock_decode = mock_jwks
    mock_signing_key = MagicMock()
    mock_signing_key.key = "fake-key"
    mock_client.return_value.get_signing_key_from_jwt.return_value = mock_signing_key
    mock_decode.return_value = {
        "sub": "bob",
        "tenant_id": "tenant-42",
        "permissions": ["USER", "ADMIN"],
    }

    request = FakeRequest("Bearer valid-token")
    await require_auth(request)

    # Verify context var is set
    assert tenant_context.get() == "tenant-42"
