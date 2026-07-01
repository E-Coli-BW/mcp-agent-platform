"""Tests for chat endpoint authentication."""

import pytest
from unittest.mock import patch, AsyncMock, MagicMock
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    """Create test client with auth middleware active."""
    from app.main import app
    return TestClient(app)


def test_chat_completions_no_auth_returns_401(client):
    """Chat endpoint without auth header should return 401."""
    resp = client.post("/v1/chat/completions", json={
        "messages": [{"role": "user", "content": "hello"}],
        "stream": False
    })
    assert resp.status_code == 401


def test_chat_completions_invalid_token_returns_401(client):
    """Chat endpoint with invalid token should return 401."""
    resp = client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "hello"}], "stream": False},
        headers={"Authorization": "Bearer invalid-token-xyz"}
    )
    assert resp.status_code == 401
