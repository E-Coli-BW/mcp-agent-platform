"""Tests for AuthServiceClient — centralized token acquisition."""

import time
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from app.auth.auth_client import AuthServiceClient


class TestAuthServiceClient:

    @pytest.mark.asyncio
    async def test_get_token_success(self):
        client = AuthServiceClient("http://localhost:8090", "agent", "secret")

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "access_token": "test-rs256-token",
            "token_type": "Bearer",
            "expires_in": 3600,
        }

        with patch("httpx.AsyncClient") as MockClient:
            mock_http = AsyncMock()
            mock_http.post = AsyncMock(return_value=mock_resp)
            mock_http.__aenter__ = AsyncMock(return_value=mock_http)
            mock_http.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = mock_http

            token = await client.get_token(audience="memory-server", tenant_id="t1")

            assert token == "test-rs256-token"
            # Verify correct params sent
            call_args = mock_http.post.call_args
            assert "client_credentials" in str(call_args)

    @pytest.mark.asyncio
    async def test_token_is_cached(self):
        client = AuthServiceClient("http://localhost:8090", "agent", "secret")
        # Pre-fill cache
        client._cache["memory-server:t1"] = ("cached-token", time.time() + 3600)

        token = await client.get_token(audience="memory-server", tenant_id="t1")
        assert token == "cached-token"

    @pytest.mark.asyncio
    async def test_expired_cache_refreshes(self):
        client = AuthServiceClient("http://localhost:8090", "agent", "secret")
        # Pre-fill with expired token
        client._cache["memory-server:t1"] = ("old-token", time.time() - 100)

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "access_token": "new-token",
            "expires_in": 3600,
        }

        with patch("httpx.AsyncClient") as MockClient:
            mock_http = AsyncMock()
            mock_http.post = AsyncMock(return_value=mock_resp)
            mock_http.__aenter__ = AsyncMock(return_value=mock_http)
            mock_http.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = mock_http

            token = await client.get_token(audience="memory-server", tenant_id="t1")
            assert token == "new-token"

    @pytest.mark.asyncio
    async def test_auth_service_unavailable_returns_none(self):
        client = AuthServiceClient("http://localhost:9999", "agent", "secret")

        with patch("httpx.AsyncClient") as MockClient:
            mock_http = AsyncMock()
            mock_http.post = AsyncMock(side_effect=Exception("Connection refused"))
            mock_http.__aenter__ = AsyncMock(return_value=mock_http)
            mock_http.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = mock_http

            token = await client.get_token()
            assert token is None
            assert client._available is False

    @pytest.mark.asyncio
    async def test_retry_after_cooldown(self):
        client = AuthServiceClient("http://localhost:8090", "agent", "secret")
        client._available = False
        client._last_check = time.time()  # just checked
        client._retry_interval = 30

        # Should return None immediately (within cooldown)
        token = await client.get_token()
        assert token is None

    def test_invalidate_clears_cache(self):
        client = AuthServiceClient("http://localhost:8090", "agent", "secret")
        client._cache = {
            "memory-server:t1": ("tok1", time.time() + 3600),
            "filesearch-server:t1": ("tok2", time.time() + 3600),
        }
        client.invalidate(audience="memory-server")
        assert len(client._cache) == 1
        assert "filesearch-server:t1" in client._cache
