"""Tests for the async JWKS fetcher (P1-4 fix).

The original implementation used `urllib.request.urlopen` inside an async
middleware, which blocks the event loop for the duration of the HTTP call.
This test suite locks in:

  1. `_fetch_jwks` is an async coroutine (not a blocking sync function).
  2. It uses httpx (not urllib).
  3. Repeated calls within the TTL hit the cache (no extra network calls).
  4. Concurrent calls during cache miss collapse into a single upstream fetch
     (stampede protection — otherwise an expiring cache causes a thundering
     herd against auth-service).
  5. `prewarm_jwks` populates the cache so the first real request is hot.
  6. On auth-service failure with no cache, we return None (caller handles 401).
  7. On auth-service failure WITH stale cache, we keep serving stale (better
     than blanket-rejecting valid traffic during a brief auth-service blip).
"""

import asyncio
import inspect

import httpx
import pytest

from app.middleware import jwt_auth


@pytest.fixture(autouse=True)
def _reset_jwks_cache():
    jwt_auth._reset_jwks_cache_for_tests()
    yield
    jwt_auth._reset_jwks_cache_for_tests()


def test_fetch_jwks_is_async_coroutine():
    """Regression guard — must NOT be a sync function (would block event loop)."""
    assert inspect.iscoroutinefunction(jwt_auth._fetch_jwks)
    assert inspect.iscoroutinefunction(jwt_auth.prewarm_jwks)
    assert inspect.iscoroutinefunction(jwt_auth._verify_rs256)


def test_no_urllib_imported_in_module():
    """We deliberately moved off urllib. Catch accidental regressions."""
    source = inspect.getsource(jwt_auth)
    assert "urllib.request" not in source, (
        "urllib.request would re-introduce the asyncio-blocking bug (P1-4)"
    )


async def test_fetch_jwks_uses_httpx(httpx_mock):
    httpx_mock.add_response(
        url="http://auth/auth/jwks",
        json={"keys": [{"kid": "k1", "kty": "RSA", "n": "AA", "e": "AQAB"}]},
    )
    result = await jwt_auth._fetch_jwks("http://auth")
    assert result is not None
    assert result["keys"][0]["kid"] == "k1"


async def test_cache_hit_skips_network(httpx_mock):
    httpx_mock.add_response(
        url="http://auth/auth/jwks",
        json={"keys": [{"kid": "k1"}]},
    )
    # First call populates cache. Second call must NOT hit the network —
    # pytest-httpx will raise at teardown if a mock is registered but unused,
    # so we register exactly one response and call twice.
    first = await jwt_auth._fetch_jwks("http://auth")
    second = await jwt_auth._fetch_jwks("http://auth")
    assert first == second


async def test_concurrent_misses_collapse_to_single_fetch(httpx_mock):
    """20 simultaneous tasks see a cold cache — only one should hit auth."""
    httpx_mock.add_response(
        url="http://auth/auth/jwks",
        json={"keys": [{"kid": "k1"}]},
    )
    results = await asyncio.gather(*[
        jwt_auth._fetch_jwks("http://auth") for _ in range(20)
    ])
    # All 20 got the same payload; only one upstream request was made
    # (pytest-httpx would fail at teardown if the single registered mock
    # were consumed twice or never).
    assert all(r["keys"][0]["kid"] == "k1" for r in results)
    assert len(httpx_mock.get_requests()) == 1


async def test_prewarm_populates_cache(httpx_mock):
    httpx_mock.add_response(
        url="http://auth/auth/jwks",
        json={"keys": [{"kid": "warm"}]},
    )
    await jwt_auth.prewarm_jwks("http://auth")
    # After prewarm, a real call must hit the cache (no second network call).
    fetched = await jwt_auth._fetch_jwks("http://auth")
    assert fetched["keys"][0]["kid"] == "warm"
    assert len(httpx_mock.get_requests()) == 1


async def test_failure_with_no_cache_returns_none(httpx_mock):
    httpx_mock.add_exception(httpx.ConnectError("auth unreachable"))
    result = await jwt_auth._fetch_jwks("http://auth")
    assert result is None


async def test_failure_with_stale_cache_returns_stale(httpx_mock):
    # Populate cache first
    httpx_mock.add_response(
        url="http://auth/auth/jwks",
        json={"keys": [{"kid": "old"}]},
    )
    await jwt_auth._fetch_jwks("http://auth")

    # Force cache "expired" so the next call attempts a refresh
    jwt_auth._jwks_cache_time = 0  # type: ignore[attr-defined]

    # Next refresh fails — must keep serving the stale copy
    httpx_mock.add_exception(httpx.ConnectError("auth blip"))
    result = await jwt_auth._fetch_jwks("http://auth")
    assert result is not None
    assert result["keys"][0]["kid"] == "old"
