"""JWT authentication middleware for the Python agent server.

Validates JWT tokens on protected endpoints (/v1/chat).
Supports two verification strategies:
  1. RS256 via JWKS (fetched from auth-service) — for tokens issued by auth-service
  2. HMAC-SHA256 with shared secret — legacy fallback

Public endpoints (/health, /docs, /api/*) are exempt.
"""

import asyncio
import hmac
import hashlib
import base64
import json
import time
import logging
import httpx
from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response, JSONResponse

logger = logging.getLogger(__name__)

# Paths that don't require authentication
PUBLIC_PATHS = {"/health", "/docs", "/openapi.json", "/redoc"}
PUBLIC_PREFIXES = ["/api/"]  # workspace endpoints are public (auth at gateway level)

# Cache for JWKS public key
_jwks_cache: dict | None = None
_jwks_cache_time: float = 0
JWKS_CACHE_TTL = 300  # 5 minutes

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
        except Exception as e:
            # If we already have a stale copy, keep using it — better than 401-ing
            # legitimate traffic when auth-service has a 30 s blip.
            if _jwks_cache:
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
    except Exception as e:  # pragma: no cover — defensive
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

        # Find matching key
        key_data = None
        for k in jwks["keys"]:
            if k.get("kid") == kid or kid is None:
                key_data = k
                break
        if not key_data:
            return None

        from jwt import algorithms
        public_key = algorithms.RSAAlgorithm.from_jwk(key_data)

        payload = pyjwt.decode(
            token, public_key, algorithms=["RS256"],
            options={"verify_aud": False}  # audience check done at app level
        )
        return payload
    except Exception as e:
        logger.debug(f"RS256 verification failed: {e}")
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
        try:
            parts = token.split(".")
            if len(parts) != 3:
                return None

            header_b64, payload_b64, signature_b64 = parts
            signing_input = f"{header_b64}.{payload_b64}"
            secret_bytes = self._secret.encode()
            if len(secret_bytes) < 32:
                secret_bytes = secret_bytes.ljust(32, b'\x00')

            expected_sig = base64.urlsafe_b64encode(
                hmac.new(secret_bytes, signing_input.encode(), hashlib.sha256).digest()
            ).rstrip(b'=').decode()

            if not hmac.compare_digest(signature_b64, expected_sig):
                expected_sig_padded = base64.urlsafe_b64encode(
                    hmac.new(secret_bytes, signing_input.encode(), hashlib.sha256).digest()
                ).decode()
                if not hmac.compare_digest(signature_b64, expected_sig_padded):
                    return None

            payload_b64_padded = payload_b64 + "=" * (4 - len(payload_b64) % 4)
            payload = json.loads(base64.urlsafe_b64decode(payload_b64_padded))

            if "exp" in payload and payload["exp"] < time.time():
                return None

            return payload
        except Exception:
            return None

    async def _verify_jwt(self, token: str) -> dict | None:
        """Try RS256 first (auth-service tokens), fall back to HMAC."""
        # Try RS256 via JWKS
        payload = await _verify_rs256(token, self._auth_url)
        if payload:
            return payload
        # Fall back to HMAC
        return self._verify_hmac(token)

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

        request.state.tenant_id = payload.get("tenant_id", payload.get("sub", "unknown"))
        request.state.jwt_subject = payload.get("sub", "unknown")

        return await call_next(request)
