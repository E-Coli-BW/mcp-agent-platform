"""FastAPI auth middleware — validates user JWTs from the auth-service.

Fetches JWKS from auth-service, verifies RS256 signatures, extracts tenant context.
"""

import logging
from contextvars import ContextVar
from dataclasses import dataclass

from fastapi import HTTPException, Request

from app.config import settings

logger = logging.getLogger(__name__)

# Context variable for tenant propagation through async call stack
tenant_context: ContextVar[str] = ContextVar("tenant_id", default="default")
_VERIFIED_CONTEXT_MARKER = object()


@dataclass
class AuthContext:
    """Verified user identity extracted from JWT."""
    user_id: str
    tenant_id: str
    permissions: list[str]


def cache_verified_auth_context(request: Request, context: AuthContext) -> None:
    """Cache identity that was produced by this request's JWT verification."""
    request.state.auth_context = context
    request.state._verified_auth_context_marker = _VERIFIED_CONTEXT_MARKER


def auth_context_from_payload(payload: dict) -> AuthContext | None:
    """Build a trusted request identity from verified JWT claims."""
    user_id = payload.get("sub")
    tenant_id = payload.get("tenant_id")
    permissions = payload.get("permissions") or []
    if not isinstance(user_id, str) or not user_id:
        return None
    if not isinstance(tenant_id, str) or not tenant_id:
        return None
    if not isinstance(permissions, list):
        return None
    return AuthContext(
        user_id=user_id,
        tenant_id=tenant_id,
        permissions=[str(permission) for permission in permissions],
    )


async def require_auth(request: Request) -> AuthContext:
    """FastAPI dependency that validates the JWT and returns AuthContext.

    Usage:
        @router.post("/endpoint")
        async def endpoint(auth: AuthContext = Depends(require_auth)):
            print(auth.tenant_id)
    """
    request_state = getattr(request, "state", None)
    verified = getattr(request_state, "auth_context", None)
    marker = getattr(request_state, "_verified_auth_context_marker", None)
    if isinstance(verified, AuthContext) and marker is _VERIFIED_CONTEXT_MARKER:
        tenant_context.set(verified.tenant_id)
        return verified

    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail={"error": "unauthorized", "detail": "Missing or invalid Authorization header"}
        )
    token = auth_header[7:]
    from app.middleware.jwt_auth import verify_access_token

    payload = await verify_access_token(
        token,
        auth_url=settings.auth_service_url,
        jwt_secret=settings.jwt_secret,
        allow_legacy_hmac=settings.allow_legacy_hmac_auth,
    )
    context = auth_context_from_payload(payload) if payload is not None else None
    if context is None:
        logger.warning("Bearer token verification failed")
        raise HTTPException(
            status_code=401,
            detail={"error": "unauthorized", "detail": "Token verification failed"},
        )
    if request_state is not None:
        cache_verified_auth_context(request, context)
    tenant_context.set(context.tenant_id)
    return context
