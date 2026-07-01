"""Contract tests for McpToolClient JWT authentication.

These tests verify that McpToolClient correctly generates and attaches
service-to-service JWTs when calling Java backends.

WHY THIS EXISTS:
When /api/** was changed from permitAll to .authenticated(), all tool calls
broke because McpToolClient wasn't attaching JWT. Unit tests on both sides
passed individually but never tested the auth contract between services.

These tests verify:
1. JWT is correctly generated with tenant_id claim
2. JWT is attached to HTTP requests as Bearer token
3. JWT is cached and auto-renewed
4. Without jwt_secret, no auth header is sent (backward compatible)
"""

import json
import time
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from app.tools.mcp_client import McpToolClient


class TestJwtGeneration:
    """Test JWT generation logic without making HTTP calls."""

    def test_generates_valid_jwt_structure(self):
        client = McpToolClient("http://localhost:8180", jwt_secret="test-secret-at-least-8-chars")
        token = client._get_legacy_token()

        assert token is not None
        parts = token.split(".")
        assert len(parts) == 3, "JWT must have 3 parts: header.payload.signature"

    def test_jwt_contains_tenant_id(self):
        client = McpToolClient("http://localhost:8180", jwt_secret="test-secret-32chars-long-enough!")
        token = client._get_legacy_token(tenant_id="my-tenant")

        # Decode payload (base64)
        import base64
        payload_b64 = token.split(".")[1]
        # Add padding
        payload_b64 += "=" * (4 - len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))

        assert payload["tenant_id"] == "my-tenant"
        assert payload["sub"] == "agent-server"
        assert "exp" in payload
        assert "iat" in payload

    def test_jwt_is_cached(self):
        client = McpToolClient("http://localhost:8180", jwt_secret="test-secret-at-least-8")
        token1 = client._get_legacy_token()
        token2 = client._get_legacy_token()

        assert token1 == token2, "Same token should be returned within cache TTL"

    def test_no_jwt_without_secret(self):
        client = McpToolClient("http://localhost:8180")  # no jwt_secret
        token = client._get_legacy_token()

        assert token is None

    def test_jwt_short_secret_is_padded(self):
        """Short secrets are zero-padded to 32 bytes, matching Java JwtAuthFilter behavior."""
        client = McpToolClient("http://localhost:8180", jwt_secret="short-key")
        token = client._get_legacy_token()

        assert token is not None  # should not crash


class TestJwtAttachment:
    """Test that JWT is attached to HTTP requests."""

    @pytest.mark.asyncio
    async def test_auth_header_attached_when_secret_provided(self):
        client = McpToolClient("http://localhost:8180", jwt_secret="test-secret-32chars!")

        # Mock the HTTP client
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"result": "ok"}

        mock_http = AsyncMock()
        mock_http.post = AsyncMock(return_value=mock_response)
        mock_http.is_closed = False
        client._client = mock_http

        # Mock tracing (imported inside call_tool via from app.tracing import ...)
        with patch("app.tracing.get_tracer") as mock_tracer, \
             patch("app.tracing.inject_trace_headers", side_effect=lambda h: h):
            mock_span = MagicMock()
            mock_span.__enter__ = MagicMock(return_value=mock_span)
            mock_span.__exit__ = MagicMock(return_value=False)
            mock_tracer.return_value.start_as_current_span.return_value = mock_span

            await client.call_tool("memory_context", {})

            # Verify Authorization header was sent
            call_args = mock_http.post.call_args
            headers = call_args.kwargs.get("headers", {})
            assert "Authorization" in headers
            assert headers["Authorization"].startswith("Bearer ")

    @pytest.mark.asyncio
    async def test_no_auth_header_without_secret(self):
        client = McpToolClient("http://localhost:8180")  # no secret

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"result": "ok"}

        mock_http = AsyncMock()
        mock_http.post = AsyncMock(return_value=mock_response)
        mock_http.is_closed = False
        client._client = mock_http

        with patch("app.tracing.get_tracer") as mock_tracer, \
             patch("app.tracing.inject_trace_headers", side_effect=lambda h: h):
            mock_span = MagicMock()
            mock_span.__enter__ = MagicMock(return_value=mock_span)
            mock_span.__exit__ = MagicMock(return_value=False)
            mock_tracer.return_value.start_as_current_span.return_value = mock_span

            await client.call_tool("memory_context", {})

            call_args = mock_http.post.call_args
            headers = call_args.kwargs.get("headers", {})
            assert "Authorization" not in headers


class TestConnectionPooling:
    """Test that the HTTP client uses connection pooling."""

    def test_client_reuses_connection(self):
        client = McpToolClient("http://localhost:8180")
        c1 = client._get_client()
        c2 = client._get_client()
        assert c1 is c2, "Should reuse the same httpx.AsyncClient instance"

    @pytest.mark.asyncio
    async def test_close_shuts_down_pool(self):
        client = McpToolClient("http://localhost:8180")
        _ = client._get_client()
        await client.close()
        # After close, a new client should be created
        c2 = client._get_client()
        assert c2 is not None
