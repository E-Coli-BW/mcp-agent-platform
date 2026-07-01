"""Tool Client — calls Java backends via REST bridge endpoints."""

import httpx
import json
import time
import hashlib
import hmac
import base64
from typing import Any, Optional


class McpToolClient:
    """HTTP client for calling tool backends via REST bridge.
    
    Authentication strategy (in order):
    1. AuthServiceClient (centralized RS256 JWT from auth service)
    2. Self-signed HMAC JWT (legacy fallback if auth service unavailable)
    3. No auth (if neither is configured)
    """

    def __init__(self, base_url: str, timeout: int = 30,
                 jwt_secret: str | None = None,
                 auth_client: Optional[Any] = None,
                 service_name: str = "agent-server",
                 audience: str = "mcp-platform"):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._jwt_secret = jwt_secret
        self._auth_client = auth_client  # AuthServiceClient instance
        self._service_name = service_name
        self._audience = audience
        self._client: httpx.AsyncClient | None = None
        # Legacy self-signed token cache
        self._cached_token: str | None = None
        self._token_expires: float = 0

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=self.timeout,
                limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            )
        return self._client

    async def _get_token(self, tenant_id: str | None = None) -> str | None:
        """Get auth token — tries auth service first, falls back to self-signed."""
        # Strategy 1: Centralized auth service (RS256)
        if self._auth_client:
            token = await self._auth_client.get_token(audience=self._audience, tenant_id=tenant_id)
            if token:
                return token

        # Strategy 2: Legacy self-signed HMAC JWT
        return self._get_legacy_token(tenant_id)

    def _get_legacy_token(self, tenant_id: str | None = None) -> str | None:
        """Legacy: self-signed HMAC-SHA256 JWT (fallback when auth service is down)."""
        if self._jwt_secret is None:
            return None

        now = time.time()
        if self._cached_token and now < self._token_expires:
            return self._cached_token

        header = base64.urlsafe_b64encode(b'{"alg":"HS256","typ":"JWT"}').rstrip(b'=').decode()
        payload_data = {
            "sub": self._service_name,
            "tenant_id": tenant_id or "default",
            "iat": int(now),
            "exp": int(now) + 3600,
            "roles": ["SERVICE"],
        }
        payload = base64.urlsafe_b64encode(
            json.dumps(payload_data).encode()
        ).rstrip(b'=').decode()

        signing_input = f"{header}.{payload}"
        secret_bytes = self._jwt_secret.encode()
        if len(secret_bytes) < 32:
            secret_bytes = secret_bytes.ljust(32, b'\x00')
        signature = base64.urlsafe_b64encode(
            hmac.new(secret_bytes, signing_input.encode(), hashlib.sha256).digest()
        ).rstrip(b'=').decode()

        self._cached_token = f"{header}.{payload}.{signature}"
        self._token_expires = now + 3300
        return self._cached_token

    async def close(self):
        """Close the underlying connection pool."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def call_tool(self, tool_name: str, arguments: dict[str, Any], tenant_id: str | None = None) -> str:
        """Call a tool via the REST bridge endpoint.
        
        Propagates OpenTelemetry trace context via W3C headers so the
        Java backend creates child spans under the same trace.

        Also injects agent fleet lineage headers (X-Root-Session-Id,
        X-Parent-Session-Id, X-Agent-Depth) so the Java AuditAspect can
        thread audit rows into a spawn tree. See
        ``app.agent.subagent_context.SubagentContext`` for the source of
        truth; mirror constants live in ``mcp-common``'s
        ``AgentLineageContext`` on the Java side.
        """
        from app.tracing import get_tracer, inject_trace_headers
        tracer = get_tracer()

        with tracer.start_as_current_span(f"mcp.{tool_name}") as span:
            span.set_attribute("tool.name", tool_name)
            span.set_attribute("tool.backend", self.base_url)

            headers = inject_trace_headers({"Content-Type": "application/json"})
            _inject_lineage_headers(headers)

            # Attach JWT (auth service RS256 or legacy HMAC fallback)
            token = await self._get_token(tenant_id=tenant_id)
            if token:
                headers["Authorization"] = f"Bearer {token}"

            client = self._get_client()
            try:
                resp = await client.post(
                    f"{self.base_url}/api/tools/{tool_name}",
                    json=arguments,
                    headers=headers,
                )
                span.set_attribute("http.status_code", resp.status_code)
                if resp.status_code == 200:
                    data = resp.json()
                    return data.get("result", json.dumps(data))
                else:
                    return f"Error: HTTP {resp.status_code} from {tool_name}"
            except httpx.ConnectError:
                span.set_attribute("error", True)
                return f"Error: Cannot connect to {self.base_url} — is the service running?"
            except Exception as e:
                span.record_exception(e)
                return f"Error calling {tool_name}: {str(e)}"


# ── Agent fleet lineage propagation ────────────────────────────────────────
# These constants MUST match com.example.mcp.common.security.AgentLineageContext
# on the Java side (pinned by AgentLineageContextTest.headerNameConstants_matchWireFormat).
# Service-to-service wire format — change in lockstep with Java or audit
# rows lose their parent edges.
HEADER_ROOT_SESSION = "X-Root-Session-Id"
HEADER_PARENT_SESSION = "X-Parent-Session-Id"
HEADER_DEPTH = "X-Agent-Depth"


def _inject_lineage_headers(headers: dict[str, str]) -> None:
    """Mutate ``headers`` in place, adding the agent-fleet lineage headers
    when a SubagentContext is bound to the current asyncio task.

    Why a separate function (not inline):
        Lets us unit-test header injection without spinning up the whole
        httpx machinery, and lets future callers (e.g. a streaming SSE
        client to the Java side) reuse the exact same logic so the audit
        contract stays single-sourced.

    Why mutate-in-place:
        Matches the pattern of ``inject_trace_headers`` which also mutates,
        so call sites compose cleanly. The header dict is local to the
        request anyway — no aliasing risk.

    Why we still emit at depth=0:
        The Java side wants to be able to draw the tree root, not just
        the edges. Emitting at depth=0 means the root request is itself
        a node in the audit graph (with parent==root, depth==0), and a
        SQL ``GROUP BY root_session_id`` recovers the whole fleet.

    Skipped when no SubagentContext exists (the import is intentionally
    lazy because not every caller of mcp_client lives inside an agent
    request — e.g. a healthcheck — and we don't want to import the agent
    module on every cold path).
    """
    try:
        # Lazy import to avoid a hard dependency from the tool-client
        # module on the agent-context module. Cheap after first call
        # (Python caches the import).
        from app.agent.subagent_context import subagent_context
    except Exception:
        # If the agent module isn't importable in this deployment
        # (e.g. mcp_client used by an out-of-process script), silently
        # skip — observability must not break the call.
        return

    ctx = subagent_context.get()
    if ctx is None:
        # ContextVar default is None — happens when called from a non-agent
        # code path (a unit test, a CLI tool, the healthcheck endpoint).
        # Don't inject headers: the Java side will record "-"/"-"/0 which
        # correctly says "no fleet lineage for this call".
        return

    headers[HEADER_ROOT_SESSION] = ctx.root_session_id
    headers[HEADER_PARENT_SESSION] = ctx.parent_session_id
    headers[HEADER_DEPTH] = str(ctx.depth)

