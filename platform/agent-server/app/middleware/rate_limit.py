"""Rate limiter middleware — per-IP and per-tenant sliding window.

Two layers of protection:
1. Per-IP: prevents anonymous abuse (before auth), default 60 req/min
2. Per-tenant: prevents noisy neighbor (after auth extracts tenant_id),
   configurable per-tenant via AGENT_TENANT_RATE_LIMITS env var (JSON map)

For production at scale with multiple workers, use Redis-backed sliding window.
This implementation uses local memory (sufficient for single-process asyncio).
"""

import json
import logging
import os
import time
from collections import defaultdict

from fastapi import Request, HTTPException
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import JSONResponse, Response

logger = logging.getLogger(__name__)

# ── Per-tenant rate limit config ──────────────────────────────
# Format: {"tenant-a": {"rpm": 120, "tpm": 100000}, "default": {"rpm": 60, "tpm": 50000}}
# rpm = requests per minute, tpm = tokens per minute (future use)
_DEFAULT_TENANT_RPM = 60
_TENANT_LIMITS: dict[str, dict] = {}

try:
    _raw = os.environ.get("AGENT_TENANT_RATE_LIMITS", "{}")
    _TENANT_LIMITS = json.loads(_raw) if _raw else {}
except (json.JSONDecodeError, TypeError):
    logger.warning("Invalid AGENT_TENANT_RATE_LIMITS JSON, using defaults")


def _get_tenant_rpm(tenant_id: str) -> int:
    """Get RPM limit for a tenant. Falls back to 'default' key, then global default."""
    if tenant_id in _TENANT_LIMITS:
        return _TENANT_LIMITS[tenant_id].get("rpm", _DEFAULT_TENANT_RPM)
    return _TENANT_LIMITS.get("default", {}).get("rpm", _DEFAULT_TENANT_RPM)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Dual-layer rate limiter: per-IP + per-tenant sliding window.

    Per-IP runs unconditionally (defense against unauthenticated abuse).
    Per-tenant runs when tenant_id is available in request state (set by
    JwtAuthMiddleware which runs before this in the middleware stack).

    Args:
        app: FastAPI application
        max_requests: Max requests per window per IP (default: 60)
        window_seconds: Window size in seconds (default: 60)
        paths: List of path prefixes to rate limit (default: ["/v1/chat"])
    """

    def __init__(self, app, max_requests: int = 60, window_seconds: int = 60,
                 paths: list[str] | None = None):
        super().__init__(app)
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.paths = paths or ["/v1/chat"]
        # Per-IP buckets
        self._ip_requests: dict[str, list[float]] = defaultdict(list)
        # Per-tenant buckets
        self._tenant_requests: dict[str, list[float]] = defaultdict(list)

    def _check_and_record(self, bucket: dict[str, list[float]], key: str,
                          limit: int, now: float) -> bool:
        """Slide window and check if limit exceeded. Returns True if allowed."""
        cutoff = now - self.window_seconds
        bucket[key] = [t for t in bucket[key] if t > cutoff]
        if len(bucket[key]) >= limit:
            return False
        bucket[key].append(now)
        return True

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        # Only rate limit configured paths
        if not any(request.url.path.startswith(p) for p in self.paths):
            return await call_next(request)

        now = time.monotonic()

        # Layer 1: Per-IP
        client_ip = request.client.host if request.client else "unknown"
        if not self._check_and_record(self._ip_requests, client_ip, self.max_requests, now):
            return JSONResponse(
                status_code=429,
                content={"detail": f"Rate limit exceeded: {self.max_requests} requests per {self.window_seconds}s"},
                headers={"Retry-After": str(self.window_seconds)},
            )

        # Layer 2: Per-tenant (only if tenant_id is available from auth middleware)
        tenant_id = getattr(request.state, "tenant_id", None)
        if tenant_id:
            tenant_rpm = _get_tenant_rpm(tenant_id)
            if not self._check_and_record(self._tenant_requests, tenant_id, tenant_rpm, now):
                return JSONResponse(
                    status_code=429,
                    content={
                        "detail": f"Tenant rate limit exceeded: {tenant_rpm} requests per {self.window_seconds}s",
                        "tenant_id": tenant_id,
                    },
                    headers={"Retry-After": str(self.window_seconds)},
                )

        return await call_next(request)
