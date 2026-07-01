"""Tests for the C2 direct-tool-routing classifier and graph node.

Three concerns, three test classes:
  TestClassifier      — pure regex / arg-validation logic, no graph
  TestRouterNode      — async node behavior (feature flag, allowlist,
                        tool availability, conversation-position gates)
  TestRouteAfterIntent — conditional-edge function

Run:
    cd platform/agent-server
    .venv/bin/python -m pytest tests/test_tool_router.py -q
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.agent.graph_v2 import AgentState
from app.agent.tool_router import (
    READ_ONLY_TOOL_ALLOWLIST,
    RoutedCall,
    classify_for_direct_dispatch,
    make_intent_router_node,
    route_after_intent_router,
)


# ── Helpers ────────────────────────────────────────────────────────────


def _fake_tool(name: str):
    """Build a minimal object that looks like a BaseTool for the
    purposes of the router's `tool_by_name = {t.name: t for t in tools}`
    construction. We don't need full tool semantics — the router only
    cares about the name."""
    t = MagicMock()
    t.name = name
    return t


@pytest.fixture
def _enable_routing(monkeypatch):
    """Flip the C2 feature flag on for the duration of the test."""
    from app.config import settings as live_settings
    monkeypatch.setattr(live_settings, "direct_tool_routing_enabled", True)


# ── 1. The pure regex classifier ──────────────────────────────────────


class TestClassifierPositives:
    """Cases that SHOULD produce a RoutedCall. These are the high-
    confidence patterns that justify shipping the router at all."""

    def test_memory_search_simple(self):
        result = classify_for_direct_dispatch("search my memory for jwt auth")
        assert result is not None
        assert result.tool_name == "memory_search"
        assert result.args == {"query": "jwt auth"}

    def test_memory_search_alternate_phrasing(self):
        for text in [
            "search memory for cache invalidation",
            "search the memory for cache invalidation",
            "find in memory database schema",
            "find memory about database schema",
            "recall what we decided about pagination",
        ]:
            result = classify_for_direct_dispatch(text)
            assert result is not None, f"failed to match: {text!r}"
            assert result.tool_name == "memory_search"

    def test_memory_context(self):
        for text in [
            "what's in my memory?",
            "what is in memory",
            "show me memory overview",
            "list my memory summary",
            "give me memory context",
        ]:
            result = classify_for_direct_dispatch(text)
            assert result is not None, f"failed to match: {text!r}"
            assert result.tool_name == "memory_context"
            assert result.args == {}

    def test_file_read_with_extension(self):
        """Path heuristic: must contain a dot or slash. Plain words
        like 'read the bug' must NOT match."""
        result = classify_for_direct_dispatch("read README.md")
        assert result is not None
        assert result.tool_name == "file_read"
        assert result.args == {"path": "README.md"}

    def test_file_read_with_slash(self):
        result = classify_for_direct_dispatch("show me src/main.py")
        assert result is not None
        assert result.tool_name == "file_read"
        assert result.args == {"path": "src/main.py"}

    def test_file_list_with_directory(self):
        result = classify_for_direct_dispatch("list files in src/")
        assert result is not None
        assert result.tool_name == "file_list"
        assert result.args == {"directory": "src/"}

    def test_file_list_no_directory(self):
        for text in [
            "list files",
            "list all files",
            "what files are there",
            "what files exist",
            "what files are in the workspace",
        ]:
            result = classify_for_direct_dispatch(text)
            assert result is not None, f"failed to match: {text!r}"
            assert result.tool_name == "file_list"
            assert result.args == {}

    def test_file_search(self):
        for text in [
            "grep for TODO",
            "grep TODO",
            "search the code for ConnectionPool",
            "search files for ConnectionPool",
        ]:
            result = classify_for_direct_dispatch(text)
            assert result is not None, f"failed to match: {text!r}"
            assert result.tool_name == "file_search"


class TestClassifierNegatives:
    """Cases that MUST fall through. False positives here would cause
    the router to misfire on real user prompts — these tests are the
    contract that protects us from that."""

    @pytest.mark.parametrize("text", [
        "",                                  # empty
        "   ",                               # whitespace only
        "tell me about jwt auth",            # not a tool pattern
        "how does the cache work?",          # general question
        "fix the bug in src/main.py",        # write intent, not read
        "save this to memory",               # write intent
        "delete README.md",                  # write intent (would be catastrophic)
        "explain how routing works",         # general question that mentions "routing"
        "I want to search my memory for something complicated",  # not start-anchored
    ])
    def test_should_not_match(self, text):
        assert classify_for_direct_dispatch(text) is None

    def test_file_read_rejects_bare_word(self):
        """No dot, no slash → not a file path. Must NOT classify as file_read."""
        assert classify_for_direct_dispatch("read the bug") is None
        assert classify_for_direct_dispatch("show me everything") is None

    def test_file_read_rejects_absolute_path(self):
        """Absolute paths are refused so we never bypass the workspace
        guard. The LLM call with file_read is still allowed because the
        tool itself rejects them via workspace_resolver."""
        assert classify_for_direct_dispatch("read /etc/passwd") is None
        assert classify_for_direct_dispatch("cat /tmp/foo.txt") is None

    def test_arg_rejects_shell_metachars(self):
        """If a regex group captures shell metacharacters we refuse to
        dispatch. Defensive — the tools themselves are not shell, but
        a wrong dispatch with weird args is a strong signal of misfire."""
        assert classify_for_direct_dispatch("search my memory for foo; rm -rf /") is None
        assert classify_for_direct_dispatch("search my memory for `whoami`") is None
        assert classify_for_direct_dispatch("search my memory for $(id)") is None

    def test_arg_rejects_traversal(self):
        assert classify_for_direct_dispatch("read ../../etc/passwd") is None


# ── 2. The router NODE (with the feature flag, allowlist, etc.) ──────


class TestIntentRouterNode:

    @pytest.mark.asyncio
    async def test_should_fallthrough_when_flagDisabled(self):
        """Default off → returns {} regardless. The whole topology can
        be present in a deployment that hasn't opted in and nothing
        changes."""
        node = make_intent_router_node([_fake_tool("memory_search")])
        state = AgentState(messages=[HumanMessage(content="search my memory for X")])
        # Note: NO _enable_routing fixture here.
        result = await node(state)
        assert result == {}

    @pytest.mark.asyncio
    async def test_should_dispatch_when_clearMatch(self, _enable_routing):
        node = make_intent_router_node([_fake_tool("memory_search")])
        state = AgentState(messages=[HumanMessage(content="search my memory for jwt")])
        result = await node(state)
        assert "messages" in result
        assert len(result["messages"]) == 1
        msg = result["messages"][0]
        assert isinstance(msg, AIMessage)
        assert msg.tool_calls
        tc = msg.tool_calls[0]
        assert tc["name"] == "memory_search"
        assert tc["args"] == {"query": "jwt"}
        # Observability marker — used by route_after_intent_router and
        # by dashboard scrapers to count router-originated calls.
        assert msg.additional_kwargs.get("router_dispatched") is True
        assert msg.additional_kwargs.get("router_rule") == "memory_search.basic"

    @pytest.mark.asyncio
    async def test_should_fallthrough_when_toolNotBound(self, _enable_routing):
        """A misconfigured agent (no memory tools) shouldn't crash —
        it should just behave as if routing were off for that query."""
        # No memory tools bound, only file tools.
        node = make_intent_router_node([_fake_tool("file_read")])
        state = AgentState(messages=[HumanMessage(content="search my memory for X")])
        result = await node(state)
        assert result == {}

    @pytest.mark.asyncio
    async def test_should_fallthrough_when_noUserMessage(self, _enable_routing):
        node = make_intent_router_node([_fake_tool("memory_search")])
        state = AgentState(messages=[])
        result = await node(state)
        assert result == {}

    @pytest.mark.asyncio
    async def test_should_fallthrough_when_lastMessageIsNotHuman(self, _enable_routing):
        """We only route on the FIRST hop of a turn. If the tail is an
        AIMessage or ToolMessage, we're mid-flow and must not interrupt
        with a synthetic tool call."""
        node = make_intent_router_node([_fake_tool("memory_search")])
        state = AgentState(messages=[
            HumanMessage(content="search my memory for X"),
            AIMessage(content="I found these results..."),
        ])
        result = await node(state)
        assert result == {}

    @pytest.mark.asyncio
    async def test_should_fallthrough_when_criticMarker(self, _enable_routing):
        """The 🪞 HumanMessage from C1 is NOT user input. Router must
        ignore it and let the existing critique loop continue."""
        node = make_intent_router_node([_fake_tool("memory_search")])
        state = AgentState(messages=[
            HumanMessage(content="search my memory for X"),
            AIMessage(content="answer"),
            HumanMessage(content="🪞 SELF-CRITIQUE (grade 2/5): revise."),
        ])
        result = await node(state)
        assert result == {}

    @pytest.mark.asyncio
    async def test_should_fallthrough_when_unmatched(self, _enable_routing):
        node = make_intent_router_node([_fake_tool("memory_search")])
        state = AgentState(messages=[HumanMessage(content="explain how cache works")])
        result = await node(state)
        assert result == {}

    @pytest.mark.asyncio
    async def test_should_warn_when_noAllowlistedToolsBound(self, _enable_routing, caplog):
        """An agent built with ONLY non-allowlisted tools (e.g. just
        memory_set) should log a warning at construction so we notice
        in production. We don't reject the build — the agent still
        works, just without the fast-path."""
        # Build with only a non-allowlisted tool.
        with caplog.at_level("INFO"):
            node = make_intent_router_node([_fake_tool("memory_set")])
        assert any("NO allowlisted read-only tools" in m for m in caplog.messages)
        # And the node never dispatches.
        state = AgentState(messages=[HumanMessage(content="search my memory for X")])
        result = await node(state)
        assert result == {}


# ── 3. The conditional-edge function ──────────────────────────────────


class TestRouteAfterIntent:

    def test_should_routeToTools_when_routerDispatched(self):
        state = AgentState(messages=[
            HumanMessage(content="search my memory for X"),
            AIMessage(
                content="",
                tool_calls=[{"id": "router_x", "name": "memory_search", "args": {"query": "X"}}],
                additional_kwargs={"router_dispatched": True, "router_rule": "memory_search.basic"},
            ),
        ])
        assert route_after_intent_router(state) == "tools"

    def test_should_routeToLLM_when_fallthrough(self):
        """Plain user message, router returned {} → last message is
        still the HumanMessage → route to call_llm."""
        state = AgentState(messages=[HumanMessage(content="explain cache")])
        assert route_after_intent_router(state) == "call_llm"

    def test_should_routeToLLM_when_resumedSessionTailAIMessage(self):
        """SUBTLE: a checkpointer-resumed session might have an
        AIMessage with tool_calls at the tail that was NOT made by
        OUR router. We must not re-execute it here — that's the job
        of the main route function downstream. The router_dispatched
        marker is the only safe signal."""
        state = AgentState(messages=[
            HumanMessage(content="something"),
            AIMessage(
                content="",
                tool_calls=[{"id": "llm_made_this", "name": "memory_search", "args": {"query": "x"}}],
                # NO router_dispatched marker.
            ),
        ])
        assert route_after_intent_router(state) == "call_llm"

    def test_should_routeToLLM_when_empty(self):
        state = AgentState(messages=[])
        assert route_after_intent_router(state) == "call_llm"


# ── 4. Allowlist invariants ───────────────────────────────────────────


class TestAllowlist:
    """Every name in READ_ONLY_TOOL_ALLOWLIST is meaningful and exists
    for a reason. These tests pin the invariants. If you add a tool to
    the allowlist, add an assertion here so future maintainers see the
    deliberate choice."""

    def test_no_write_tools_in_allowlist(self):
        """Write/exec tools must NEVER be in the allowlist — that's
        the entire point of C2's design. A drift here is a security
        regression."""
        forbidden = {
            "memory_set", "memory_delete",
            "file_write", "file_edit", "file_delete",
            "code_run", "shell", "exec_python", "exec_bash",
            "ticket_create", "ticket_update",
            "git_commit", "git_push",
        }
        leaked = forbidden & READ_ONLY_TOOL_ALLOWLIST
        assert not leaked, f"write/exec tools in allowlist: {leaked}"

    def test_allowlist_is_frozenset(self):
        """Must be immutable so a misguided patch can't mutate it at
        runtime to add a write tool."""
        assert isinstance(READ_ONLY_TOOL_ALLOWLIST, frozenset)


# ── 5. RoutedCall dataclass ──────────────────────────────────────────


class TestRoutedCall:

    def test_is_frozen(self):
        """RoutedCall is the contract object between classifier and
        router node. Freezing it prevents accidental in-place mutation
        of the args dict shared between threads."""
        call = RoutedCall("memory_search", {"query": "x"}, rule_id="r")
        with pytest.raises(Exception):
            call.tool_name = "memory_set"  # type: ignore[misc]
