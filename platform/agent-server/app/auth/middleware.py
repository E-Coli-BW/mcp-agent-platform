"""FastAPI auth middleware — validates user JWTs from the auth-service.

Fetches JWKS from auth-service, verifies RS256 signatures, extracts tenant context.
"""

import time
import logging
from dataclasses import dataclass
from contextvars import ContextVar
from typing import Optional

import httpx
import jwt  # PyJWT
from jwt import PyJWKClient
from fastapi import Request, HTTPException

from app.config import settings

logger = logging.getLogger(__name__)

# Context variable for tenant propagation through async call stack
tenant_context: ContextVar[str] = ContextVar("tenant_id", default="default")


@dataclass
class AuthContext:
    """Verified user identity extracted from JWT."""
    user_id: str
    tenant_id: str
    permissions: list[str]


# JWKS cache
_jwks_client: Optional[PyJWKClient] = None
_jwks_last_refresh: float = 0
_JWKS_CACHE_TTL: float = 300  # 5 minutes


def _get_jwks_client() -> PyJWKClient:
    """Get or create a PyJWKClient with 5-minute cache."""
    global _jwks_client, _jwks_last_refresh
    now = time.time()
    if _jwks_client is None or (now - _jwks_last_refresh) > _JWKS_CACHE_TTL:
        jwks_url = f"{settings.auth_service_url}/auth/jwks"
        _jwks_client = PyJWKClient(jwks_url, cache_keys=True)
        _jwks_last_refresh = now
    return _jwks_client


async def require_auth(request: Request) -> AuthContext:
    """FastAPI dependency that validates the JWT and returns AuthContext.

    Usage:
        @router.post("/endpoint")
        async def endpoint(auth: AuthContext = Depends(require_auth)):
            print(auth.tenant_id)
    """
    # 1. Extract token from Authorization header
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail={"error": "unauthorized", "detail": "Missing or invalid Authorization header"}
        )
    token = auth_header[7:]  # Strip "Bearer "

    # 2. Verify JWT signature and decode claims
    #    Try RS256 (auth-service JWKS) first, fall back to HMAC (dev secret)
    payload = None

    # RS256 via JWKS
    try:
        jwks_client = _get_jwks_client()
        signing_key = jwks_client.get_signing_key_from_jwt(token)
        payload = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience="agent-server",
            issuer="mcp-auth-service",
        )
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=401,
            detail={"error": "unauthorized", "detail": "Token has expired"}
        )
    except jwt.InvalidTokenError as e:
        logger.debug("RS256 verification failed, trying HMAC: %s", e)
    except Exception as e:
        logger.debug("JWKS unavailable, trying HMAC fallback: %s", e)

    # HMAC fallback (for local dev without auth-service)
    if payload is None:
        try:
            payload = jwt.decode(
                token,
                settings.jwt_secret,
                algorithms=["HS256"],
                options={"verify_aud": False, "verify_iss": False},
            )
        except jwt.ExpiredSignatureError:
            raise HTTPException(
                status_code=401,
                detail={"error": "unauthorized", "detail": "Token has expired"}
            )
        except Exception as e:
            logger.warning("JWT verification failed (both RS256 and HMAC): %s", e)
            raise HTTPException(
                status_code=401,
                detail={"error": "unauthorized", "detail": "Token verification failed"}
            )

    # 3. Extract claims
    user_id = payload.get("sub", "")
    tenant_id = payload.get("tenant_id", "default")
    permissions = payload.get("permissions", [])

    # 4. Set context variable for downstream propagation
    tenant_context.set(tenant_id)

    return AuthContext(user_id=user_id, tenant_id=tenant_id, permissions=permissions)
