"""Contract tests for agent-fleet lineage header propagation.

When Python ``agent-server`` calls a Java backend via ``McpToolClient``,
it must inject three HTTP headers describing the calling fleet's lineage:

    X-Root-Session-Id   — original chat session_id (constant per fleet)
    X-Parent-Session-Id — immediate parent's session_id
    X-Agent-Depth       — 0 for root, 1 for first child, ...

The Java side reads these in ``com.example.mcp.common.security.AgentLineageContext``
and ``AuditAspect`` writes them into the audit log. The two sides MUST
agree on header names; both pin them as constants and have tests that lock
those names (``AgentLineageContextTest.headerNameConstants_matchWireFormat``
on Java, this file's ``test_header_name_constants_match_wire_format`` on
Python).

These tests are pure unit — they mock the httpx layer the same way
``test_auth_contract.py`` does so we never touch a network.
"""

import pytest
from contextvars import copy_context
from unittest.mock import AsyncMock, MagicMock, patch

from app.tools.mcp_client import (
    McpToolClient,
    HEADER_ROOT_SESSION,
    HEADER_PARENT_SESSION,
    HEADER_DEPTH,
)


# ── Header name constants — wire-format contract ──────────────────────────


def test_header_name_constants_match_wire_format():
    """Pin the on-the-wire header names. If you rename one of these you
    MUST update the Java-side mirror in lockstep:
        platform/mcp-common/src/main/java/com/example/mcp/common/security/AgentLineageContext.java
    The Java side has the symmetric test
    ``AgentLineageContextTest.headerNameConstants_matchWireFormat``.
    """
    assert HEADER_ROOT_SESSION == "X-Root-Session-Id"
    assert HEADER_PARENT_SESSION == "X-Parent-Session-Id"
    assert HEADER_DEPTH == "X-Agent-Depth"


# ── Helper: mock a full call_tool() invocation and return the sent headers


def _make_mocked_client() -> tuple[McpToolClient, AsyncMock]:
    """Build a McpToolClient whose HTTP layer is a MagicMock.

    Returns (client, mock_http). After calling ``await client.call_tool(...)``
    the caller can inspect ``mock_http.post.call_args.kwargs['headers']``
    to assert what went over the wire.

    We bypass tracing+auth here because they're tested separately in
    ``test_auth_contract.py``; we patch them out so the only signal in the
    captured headers is our lineage logic.
    """
    client = McpToolClient("http://localhost:8180")  # no jwt_secret → no Auth header

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"result": "ok"}

    mock_http = AsyncMock()
    mock_http.post = AsyncMock(return_value=mock_response)
    mock_http.is_closed = False
    client._client = mock_http  # type: ignore[assignment]
    return client, mock_http


async def _call_and_capture_headers(client: McpToolClient, mock_http: AsyncMock) -> dict[str, str]:
    """Invoke call_tool and return whatever headers were posted."""
    # Patch tracing to a no-op span — we don't care about trace context here.
    with patch("app.tracing.get_tracer") as mock_tracer, \
         patch("app.tracing.inject_trace_headers", side_effect=lambda h: h):
        mock_span = MagicMock()
        mock_span.__enter__ = MagicMock(return_value=mock_span)
        mock_span.__exit__ = MagicMock(return_value=False)
        mock_tracer.return_value.start_as_current_span.return_value = mock_span

        await client.call_tool("memory_context", {})

    return mock_http.post.call_args.kwargs.get("headers", {})


# ── Behavior: ContextVar present ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_root_context_injects_all_three_headers():
    """A request initialized via init_root_context propagates root/parent/depth
    on every outbound tool call."""
    from app.agent.subagent_context import init_root_context

    # Run inside a fresh contextvars context so we don't leak the
    # SubagentContext into other tests (asyncio.run already does this for
    # the test body, but being explicit documents the intent).
    init_root_context(
        root_session_id="chat-7",
        allowed_tools=["memory_context"],
    )

    client, mock_http = _make_mocked_client()
    headers = await _call_and_capture_headers(client, mock_http)

    assert headers[HEADER_ROOT_SESSION] == "chat-7"
    assert headers[HEADER_PARENT_SESSION] == "chat-7", \
        "depth=0 root: parent_session_id == root_session_id"
    assert headers[HEADER_DEPTH] == "0", "root request is depth=0"


@pytest.mark.asyncio
async def test_child_context_injects_depth_and_immediate_parent():
    """Inside a subagent (depth>=1), the headers must reflect the child's
    immediate parent — not the root — so the AuditAspect can draw the
    correct parent→child edge in the spawn tree."""
    from dataclasses import replace
    from app.agent.subagent_context import init_root_context, subagent_context

    init_root_context(
        root_session_id="chat-7",
        allowed_tools=["memory_context"],
    )
    root = subagent_context.get()
    assert root is not None

    # Simulate a subagent context (depth=2: root -> child -> grandchild)
    child = replace(
        root,
        parent_session_id="chat-7/sub-abc",  # the IMMEDIATE parent's id
        depth=2,
    )
    token = subagent_context.set(child)
    try:
        client, mock_http = _make_mocked_client()
        headers = await _call_and_capture_headers(client, mock_http)
    finally:
        subagent_context.reset(token)

    assert headers[HEADER_ROOT_SESSION] == "chat-7", "root stays constant"
    assert headers[HEADER_PARENT_SESSION] == "chat-7/sub-abc", \
        "must use IMMEDIATE parent, not root"
    assert headers[HEADER_DEPTH] == "2"


# ── Behavior: ContextVar absent ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_context_omits_lineage_headers():
    """When called outside an agent request (e.g. a healthcheck or a CLI
    script), no lineage headers are sent. The Java side will record "-"/"-"/0
    which correctly says "no fleet lineage for this call"."""

    async def run_without_context():
        # Build the client and run the call without ever calling
        # init_root_context. Inside this fresh contextvars context the
        # ContextVar's default (None) is what's visible.
        client, mock_http = _make_mocked_client()
        return await _call_and_capture_headers(client, mock_http)

    # copy_context() gives us a frozen snapshot of the *current* context;
    # we want a FRESH context with no SubagentContext bound. The simplest
    # way is to clear the ContextVar explicitly.
    from app.agent.subagent_context import subagent_context

    token = subagent_context.set(None)  # type: ignore[arg-type]
    try:
        headers = await run_without_context()
    finally:
        subagent_context.reset(token)

    assert HEADER_ROOT_SESSION not in headers
    assert HEADER_PARENT_SESSION not in headers
    assert HEADER_DEPTH not in headers


# ── Defensive: tracing+auth headers coexist with lineage ─────────────────


@pytest.mark.asyncio
async def test_lineage_headers_dont_collide_with_other_headers():
    """Sanity check: when all three header families are present (Content-Type,
    trace context placeholders, auth, lineage) they all survive."""
    from app.agent.subagent_context import init_root_context

    init_root_context(
        root_session_id="chat-99",
        allowed_tools=["memory_context"],
    )

    # Use a client WITH a jwt secret so the Authorization header is also added.
    client = McpToolClient("http://localhost:8180", jwt_secret="test-secret-32chars-long-enough!")

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"result": "ok"}

    mock_http = AsyncMock()
    mock_http.post = AsyncMock(return_value=mock_response)
    mock_http.is_closed = False
    client._client = mock_http  # type: ignore[assignment]

    with patch("app.tracing.get_tracer") as mock_tracer, \
         patch("app.tracing.inject_trace_headers", side_effect=lambda h: h):
        mock_span = MagicMock()
        mock_span.__enter__ = MagicMock(return_value=mock_span)
        mock_span.__exit__ = MagicMock(return_value=False)
        mock_tracer.return_value.start_as_current_span.return_value = mock_span

        await client.call_tool("memory_context", {})

    headers = mock_http.post.call_args.kwargs["headers"]

    # All three header families present and well-formed
    assert headers["Content-Type"] == "application/json"
    assert headers["Authorization"].startswith("Bearer ")
    assert headers[HEADER_ROOT_SESSION] == "chat-99"
    assert headers[HEADER_PARENT_SESSION] == "chat-99"
    assert headers[HEADER_DEPTH] == "0"
