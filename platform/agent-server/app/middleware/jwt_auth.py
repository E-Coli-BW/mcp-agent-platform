"""JWT authentication middleware for the Python agent server.

Validates JWT tokens on protected endpoints (/v1/chat).
Supports two verification strategies:
  1. RS256 via JWKS (fetched from auth-service) — for tokens issued by auth-service
  2. HMAC-SHA256 with shared secret — legacy fallback

Public endpoints (/health, /docs, /api/*) are exempt.
"""

import asyncio
import logging
import time

import httpx
from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import JSONResponse, Response

logger = logging.getLogger(__name__)

# Paths that don't require authentication
PUBLIC_PATHS = {"/health", "/docs", "/openapi.json", "/redoc"}
PUBLIC_PREFIXES = ["/api/"]  # sensitive /api routes enforce route-level dependencies

# Cache for JWKS public key
_jwks_cache: dict | None = None
_jwks_cache_time: float = 0
JWKS_CACHE_TTL = 300  # 5 minutes
JWKS_MAX_STALE_SECONDS = 3600

# Per-process lock — serialises concurrent refreshes so we don't stampede
# auth-service with N parallel JWKS GETs whenever the cache expires.
_jwks_refresh_lock = asyncio.Lock()


async def _fetch_jwks(auth_url: str) -> dict | None:
    """Fetch JWKS from auth-service. Cached for 5 minutes.

    Async-only — calling blocking urllib here would stall the asyncio event
    loop for the duration of the HTTP round-trip (up to 3 s on first call /
    cache miss), starving every other concurrent request (including K8s
    health checks). See `prewarm_jwks` for the startup pre-fetch path.
    """
    global _jwks_cache, _jwks_cache_time
    if _jwks_cache and (time.time() - _jwks_cache_time) < JWKS_CACHE_TTL:
        return _jwks_cache

    # Stampede protection: only one task may refresh at a time. Other tasks
    # await the lock and then re-check the cache (which the winner populated).
    async with _jwks_refresh_lock:
        if _jwks_cache and (time.time() - _jwks_cache_time) < JWKS_CACHE_TTL:
            return _jwks_cache
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                resp = await client.get(f"{auth_url}/auth/jwks")
                resp.raise_for_status()
                _jwks_cache = resp.json()
                _jwks_cache_time = time.time()
                logger.info("Fetched JWKS from auth-service (%d keys)",
                            len(_jwks_cache.get("keys", [])))
                return _jwks_cache
        except Exception as e:  # noqa: BLE001 - network/cache boundary serves stale keys
            # If we already have a stale copy, keep using it — better than 401-ing
            # legitimate traffic when auth-service has a 30 s blip.
            if (
                _jwks_cache
                and (time.time() - _jwks_cache_time) <= JWKS_MAX_STALE_SECONDS
            ):
                logger.warning("JWKS refresh failed (%s); using stale cache", e)
                return _jwks_cache
            logger.warning("JWKS fetch failed and no cached copy: %s", e)
            return None


async def prewarm_jwks(auth_url: str) -> None:
    """Eagerly populate the JWKS cache at startup.

    Call this from FastAPI's lifespan handler so the first /v1/chat request
    doesn't have to pay the 3 s auth-service round-trip itself. We swallow
    errors here on purpose — the regular `_fetch_jwks` path will retry on
    the first real request if auth-service was momentarily down at boot.
    """
    try:
        await _fetch_jwks(auth_url)
    except Exception as e:  # noqa: BLE001  # pragma: no cover - startup fails open
        logger.warning("JWKS prewarm failed (will retry on first request): %s", e)


def _reset_jwks_cache_for_tests() -> None:
    """Test-only hook — clears the module-level cache between tests."""
    global _jwks_cache, _jwks_cache_time
    _jwks_cache = None
    _jwks_cache_time = 0


async def _verify_rs256(token: str, auth_url: str) -> dict | None:
    """Verify RS256 JWT using JWKS from auth-service."""
    try:
        import jwt as pyjwt  # PyJWT with crypto extras
        jwks = await _fetch_jwks(auth_url)
        if not jwks or "keys" not in jwks:
            return None

        # Get the signing key from JWKS
        header = pyjwt.get_unverified_header(token)
        kid = header.get("kid")
        if not isinstance(kid, str) or not kid:
            return None

        # Find matching key
        key_data = None
        for k in jwks["keys"]:
            if k.get("kid") == kid:
                key_data = k
                break
        if not key_data:
            return None

        from jwt import algorithms
        public_key = algorithms.RSAAlgorithm.from_jwk(key_data)

        payload = pyjwt.decode(
            token,
            public_key,
            algorithms=["RS256"],
            audience="agent-server",
            issuer="mcp-auth-service",
            options={"require": ["exp", "iat", "sub", "tenant_id", "jti"]},
        )
        return payload
    except Exception as e:  # noqa: BLE001 - third-party JWT/JWK failures fail closed
        logger.debug(f"RS256 verification failed: {e}")
        return None


def _verify_hmac(token: str, secret: str) -> dict | None:
    """Verify one legacy local-development HS256 token."""
    try:
        secret_bytes = secret.encode()
        if len(secret_bytes) < 32:
            secret_bytes = secret_bytes.ljust(32, b"\x00")
        import jwt as pyjwt

        return pyjwt.decode(
            token,
            secret_bytes,
            algorithms=["HS256"],
            audience="agent-server",
            issuer="mcp-agent-local",
            options={"require": ["exp", "iat", "sub", "tenant_id", "jti"]},
        )
    except Exception:  # noqa: BLE001 - malformed/expired local tokens fail closed
        return None


async def verify_access_token(
    token: str,
    *,
    auth_url: str,
    jwt_secret: str,
    allow_legacy_hmac: bool = False,
) -> dict | None:
    """Verify a bearer token using the shared process-wide JWKS cache."""
    try:
        import jwt as pyjwt

        algorithm = pyjwt.get_unverified_header(token).get("alg")
    except Exception:  # noqa: BLE001 - malformed tokens fail closed
        return None
    if algorithm == "RS256":
        return await _verify_rs256(token, auth_url)
    if algorithm == "HS256" and allow_legacy_hmac:
        return _verify_hmac(token, jwt_secret)
    return None


class JwtAuthMiddleware(BaseHTTPMiddleware):
    """Validates JWT Bearer tokens on protected endpoints.
    
    Tries RS256 (JWKS from auth-service) first, falls back to HMAC-SHA256.
    Extracts tenant_id from the JWT payload and adds it to request.state.
    """

    def __init__(self, app, jwt_secret: str, protected_prefixes: list[str] | None = None,
                 auth_service_url: str = "http://localhost:8090"):
        super().__init__(app)
        self._secret = jwt_secret
        self._protected = protected_prefixes or ["/v1/"]
        self._auth_url = auth_service_url

    def _is_protected(self, path: str) -> bool:
        if path in PUBLIC_PATHS:
            return False
        if any(path.startswith(p) for p in PUBLIC_PREFIXES):
            return False
        return any(path.startswith(p) for p in self._protected)

    def _verify_hmac(self, token: str) -> dict | None:
        """Verify HMAC-SHA256 JWT (legacy)."""
        return _verify_hmac(token, self._secret)

    async def _verify_jwt(self, token: str) -> dict | None:
        from app.config import settings

        return await verify_access_token(
            token,
            auth_url=self._auth_url,
            jwt_secret=self._secret,
            allow_legacy_hmac=settings.allow_legacy_hmac_auth,
        )

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if not self._is_protected(request.url.path):
            return await call_next(request)

        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return JSONResponse(
                status_code=401,
                content={"error": "Authentication required. Provide Bearer token."}
            )

        token = auth_header[7:]
        payload = await self._verify_jwt(token)
        if payload is None:
            return JSONResponse(
                status_code=401,
                content={"error": "Invalid or expired JWT token."}
            )

        from app.auth.middleware import (
            auth_context_from_payload,
            cache_verified_auth_context,
            tenant_context,
        )

        auth_context = auth_context_from_payload(payload)
        if auth_context is None:
            return JSONResponse(
                status_code=401,
                content={"error": "Token is missing required identity claims."},
            )
        cache_verified_auth_context(request, auth_context)
        request.state.tenant_id = auth_context.tenant_id
        request.state.jwt_subject = auth_context.user_id
        tenant_token = tenant_context.set(auth_context.tenant_id)
        try:
            return await call_next(request)
        finally:
            tenant_context.reset(tenant_token)
