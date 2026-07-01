"""Auth Service client — requests JWT tokens from the centralized auth service.

Replaces the old self-signed JWT approach in McpToolClient.
Tokens are cached per audience+tenant and auto-renewed before expiry.
Falls back to self-signed JWT if auth service is unavailable (graceful degradation).
"""

import httpx
import time
import logging
from typing import Optional

logger = logging.getLogger(__name__)


class AuthServiceClient:
    """Requests tokens from the centralized Auth Service.
    
    Usage:
        auth = AuthServiceClient("http://localhost:8090", "agent-server", "agent-secret")
        token = await auth.get_token(audience="memory-server", tenant_id="t1")
        headers = {"Authorization": f"Bearer {token}"}
    """

    def __init__(self, auth_url: str, client_id: str, client_secret: str):
        self.auth_url = auth_url.rstrip("/")
        self.client_id = client_id
        self.client_secret = client_secret
        self._cache: dict[str, tuple[str, float]] = {}  # key → (token, expiry)
        self._available: bool | None = None
        self._last_check: float = 0
        self._retry_interval: float = 30  # retry every 30s if unavailable

    async def get_token(self, audience: str = "mcp-platform",
                        tenant_id: str | None = None) -> Optional[str]:
        """Get a valid token for the target audience.
        
        Returns cached token if still valid (with 60s margin).
        Requests a new token from auth service if cache miss or expired.
        Returns None if auth service is unavailable.
        """
        cache_key = f"{audience}:{tenant_id or 'default'}"

        # Check cache
        if cache_key in self._cache:
            token, exp = self._cache[cache_key]
            if time.time() < exp - 60:  # refresh 60s before expiry
                return token

        # Check if auth service was recently unavailable
        now = time.time()
        if self._available is False and (now - self._last_check) < self._retry_interval:
            return None

        # Request new token
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                # NOTE: auth-service expects JSON (@RequestBody TokenRequest),
                # NOT OAuth2 form-encoded body. Sending form data here causes
                # HTTP 415 Unsupported Media Type and then HTTP 401 from the
                # downstream MCP backend (because we never get a service token).
                # Field names are snake_case — the Java DTO uses @JsonProperty
                # mappings (see auth-service/api/TokenRequest.java).
                payload = {
                    "grant_type": "client_credentials",
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "audience": audience,
                }
                if tenant_id:
                    payload["tenant_id"] = tenant_id

                resp = await client.post(
                    f"{self.auth_url}/auth/token",
                    json=payload,
                )

                if resp.status_code == 200:
                    body = resp.json()
                    token = body["access_token"]
                    expires_in = body.get("expires_in", 3600)
                    self._cache[cache_key] = (token, time.time() + expires_in)
                    self._available = True
                    logger.debug("Token obtained for audience=%s tenant=%s", audience, tenant_id)
                    return token
                else:
                    logger.warning("Auth service returned %d: %s", resp.status_code, resp.text)
                    self._available = True  # service is up, just auth failed
                    return None

        except Exception as e:
            logger.info("Auth service unavailable at %s: %s", self.auth_url, e)
            self._available = False
            self._last_check = now
            return None

    def invalidate(self, audience: str | None = None):
        """Clear cached tokens. Call on 401 response from a backend."""
        if audience:
            self._cache = {k: v for k, v in self._cache.items() if not k.startswith(audience)}
        else:
            self._cache.clear()
