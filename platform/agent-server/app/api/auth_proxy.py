"""
Auth proxy router — transparent reverse proxy from /auth/* to the auth-service.

WHY THIS EXISTS
===============
The embedded chat UI at /ui/ (served by this same agent process from
app/static/index.html) uses RELATIVE fetch URLs like `/auth/signup` and
`/auth/login`. With the page loaded at http://localhost:8580/ui/, those
requests go to http://localhost:8580/auth/* — i.e. THIS service.

Before this router existed, those requests 404'd. The user would type a
username + password, hit "Create Account", and see a generic "Signup
failed" message with no clue why. Symptom-identical to the auth-service
returning "invalid credentials" — but the request never even reached
auth-service.

WHY NOT JUST USE ABSOLUTE URLs IN THE UI?
=========================================
Because the auth-service URL is environment-dependent. The React
frontend at :3000 solves this with Vite's dev-server proxy
(see platform/frontend/vite.config.ts). The embedded UI has no Vite
in front of it, so we proxy here. Same outcome, same one-origin model,
no CORS to configure for the browser.

WHY NOT TWEAK CORS ON AUTH-SERVICE INSTEAD?
============================================
We're doing that too (separate change), but it doesn't help the
embedded UI: the UI's fetch('/auth/signup') is a same-origin request
to :8580. It never reaches :8090 in the first place. CORS would only
help if we changed the UI to use absolute http://localhost:8090/...
URLs, which we don't want (see above).

DESIGN NOTES
============
- Forwards method, body, content-type, and Authorization header.
- Returns the upstream status code verbatim (including 4xx/5xx so the UI
  shows the real error).
- Strips hop-by-hop headers per RFC 7230 §6.1 on the way back.
- 5s timeout. Auth calls should be < 1s; anything longer is a problem
  we want surfaced quickly.
- Uses a module-level httpx.AsyncClient with connection pooling.
"""

from __future__ import annotations

import logging
from typing import Final

import httpx
from fastapi import APIRouter, Request, Response

from app.config import settings

logger = logging.getLogger(__name__)

router = APIRouter()

# Hop-by-hop headers per RFC 7230 §6.1 — must NOT be forwarded.
# (Plus a couple of headers that httpx/uvicorn set themselves and we'd
# duplicate if we passed them through.)
_HOP_BY_HOP: Final[frozenset[str]] = frozenset(
    h.lower()
    for h in (
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "content-length",  # uvicorn sets this from the body we return
        "content-encoding",  # we don't re-encode
        "host",  # we set our own
    )
)

# Single shared client — reused across requests for connection pooling.
# Don't tighten the timeout: signup with bcrypt + DB write can take >1s
# on a cold start.
_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    """Lazy-init the shared HTTP client (avoids creating it at import time,
    which would require an event loop)."""
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=5.0)
    return _client


@router.api_route(
    "/auth/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
)
async def proxy_auth(path: str, request: Request) -> Response:
    """Forward any /auth/* request to the configured auth-service."""
    upstream = f"{settings.auth_service_url.rstrip('/')}/auth/{path}"

    # Pass through request headers, dropping hop-by-hop ones.
    fwd_headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in _HOP_BY_HOP
    }

    # Read the body once. For /auth/* this is always small (JSON payloads),
    # so buffering is fine; if we ever proxy large uploads this should
    # switch to streaming.
    body = await request.body()

    try:
        upstream_resp = await _get_client().request(
            method=request.method,
            url=upstream,
            headers=fwd_headers,
            content=body,
            params=request.query_params,
        )
    except httpx.RequestError as e:
        # Connection refused, DNS failure, timeout, etc. Don't pretend the
        # request worked. The UI shows this verbatim so the user knows
        # auth-service is the problem, not their password.
        logger.warning("auth proxy: upstream %s failed: %s", upstream, e)
        return Response(
            content=b'{"error":"Auth service unavailable"}',
            status_code=503,
            media_type="application/json",
        )

    # Strip hop-by-hop response headers too.
    resp_headers = {
        k: v
        for k, v in upstream_resp.headers.items()
        if k.lower() not in _HOP_BY_HOP
    }

    return Response(
        content=upstream_resp.content,
        status_code=upstream_resp.status_code,
        headers=resp_headers,
        media_type=upstream_resp.headers.get("content-type"),
    )
