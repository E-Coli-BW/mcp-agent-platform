"""C2 — Tool router: direct-dispatch for obvious one-tool queries.

When the user's message is clearly a one-tool read-only operation
(e.g. "search my memory for X", "list files in /tmp", "read README.md"),
we can skip the first call_llm hop and dispatch the tool call directly.
The LLM still sees the tool result on the next hop and decides whether
to continue.

WHY this design:
==========================================

1. CONSERVATIVE BY DEFAULT.
   The router only fires on high-confidence patterns. Any ambiguity
   falls through to the LLM. A wrong direct-dispatch wastes one tool
   call AND user trust ("why did it grep when I asked it to think?").
   A missed direct-dispatch wastes ~$0.02 and 800ms. The asymmetry says:
   when in doubt, route to the LLM.

2. READ-ONLY ALLOWLIST.
   We NEVER auto-route writes (memory_set, file_write) or execution
   (code_run, shell). Even if the user clearly asks "save X to memory",
   we let the LLM intermediate so it can sanity-check the args. The
   blast radius of a wrong write is unbounded; the blast radius of a
   wrong read is just one wasted tool call.

3. NO LLM IN THE ROUTER.
   Pure regex. Adds ZERO latency on the hot path. If we ever need
   semantic routing, we can add an LLM-router behind another feature
   flag and A/B them via classifier_eval.py — but the regex path stays
   as the always-on baseline because it's debuggable and free.

4. OFF BY DEFAULT (``direct_tool_routing_enabled = False``).
   Same opt-in stance as C1 reflexion. Add the topology, gate the
   behavior. Zero behavior change for existing deployments.

5. EXTRACT-VALIDATE-OR-FALLBACK.
   For every rule we (a) regex-match intent, (b) extract args via a
   named group, (c) validate the extracted args (non-empty, length
   bounded, no shell metacharacters), and (d) confirm the target tool
   exists in the bound tool list. If ANY step fails, fall through to
   the LLM. This makes the router fail-safe by construction.

GRAPH INTEGRATION (graph_v2.py):
================================

    START
      │
      ▼
    inject_context
      │
      ▼
    intent_router  ── (routed) ──► tools ──► track_errors ──► compress_history ──► call_llm ─► ...
      │
      └── (fallthrough) ──► call_llm ──► maybe_critique ──► route ──► [tools | call_llm | END]

The router node returns either:
    {}                                          # fallthrough → call_llm
    {"messages": [AIMessage(tool_calls=[...])]} # dispatched → tools

A conditional edge after intent_router checks "did the last message become
an AIMessage with tool_calls?" and routes accordingly.
"""
from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass
from typing import Any, Callable

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.tools import BaseTool

logger = logging.getLogger(__name__)


# ── Allowlist ────────────────────────────────────────────────────────────
# Tools that the router is permitted to dispatch directly. EVERYTHING
# else (memory_set, file_write, code_run, shell, ticket_create, ...)
# MUST go through the LLM so it can validate args. This is enforced
# at dispatch time — adding a tool name here is a deliberate act.
#
# Property: every name here MUST be a pure read with bounded side effects
# (no filesystem mutation, no network mutation, no DB writes). If a tool
# is reclassified to add a side effect, REMOVE it from this list.
READ_ONLY_TOOL_ALLOWLIST: frozenset[str] = frozenset({
    "memory_search",
    "memory_context",
    "memory_get",
    "skill_get",
    "file_search",
    "file_read",
    "file_list",
    "rag_search",
})

# Maximum length for an extracted argument. If a regex match grabs more
# than this we treat it as a misfire and fall through to the LLM.
# 200 chars is enough for any realistic search query, file path, or key.
_MAX_ARG_LEN = 200

# Shell-metachar / path-traversal blocklist for extracted args. If any
# of these appears in an extracted value we refuse to dispatch — the
# LLM call is cheap insurance against accidentally constructing a
# malicious tool call from regex group capture.
_DANGEROUS_ARG_CHARS = re.compile(r"[`$;<>|&\n\r\x00]|\.\./")


@dataclass(frozen=True)
class RoutedCall:
    """A regex-matched single-tool call ready to dispatch."""

    tool_name: str
    args: dict[str, Any]
    rule_id: str  # which router rule fired, for observability


# ── Rule definitions ────────────────────────────────────────────────────
# Each rule is (rule_id, compiled regex, builder). The builder takes the
# regex match and the user text and returns either a RoutedCall or None
# (None = matched but couldn't safely extract args → fallthrough).
#
# Patterns are deliberately STRICT. A few false-positives in the LLM
# fallthrough path are vastly cheaper than a false-positive direct
# dispatch.

_Rule = tuple[str, re.Pattern[str], Callable[[re.Match[str], str], RoutedCall | None]]


def _validate_arg(value: str) -> str | None:
    """Strip / validate an extracted argument. Returns None on rejection."""
    if not value:
        return None
    value = value.strip().strip('"').strip("'").strip()
    if not value:
        return None
    if len(value) > _MAX_ARG_LEN:
        return None
    if _DANGEROUS_ARG_CHARS.search(value):
        return None
    return value


def _build_memory_search(m: re.Match[str], _text: str) -> RoutedCall | None:
    query = _validate_arg(m.group("query"))
    if not query:
        return None
    return RoutedCall("memory_search", {"query": query}, rule_id="memory_search.basic")


def _build_memory_context(_m: re.Match[str], _text: str) -> RoutedCall | None:
    # No args needed — memory_context() takes nothing.
    return RoutedCall("memory_context", {}, rule_id="memory_context.basic")


def _build_file_read(m: re.Match[str], _text: str) -> RoutedCall | None:
    path = _validate_arg(m.group("path"))
    if not path:
        return None
    # Defensive: refuse absolute paths so we never bypass the
    # workspace_resolver guard. Relative paths only.
    if path.startswith("/"):
        return None
    return RoutedCall("file_read", {"path": path}, rule_id="file_read.basic")


def _build_file_list_with_directory(m: re.Match[str], _text: str) -> RoutedCall | None:
    directory = _validate_arg(m.group("directory"))
    if not directory or directory.startswith("/"):
        return None
    return RoutedCall(
        "file_list", {"directory": directory}, rule_id="file_list.directory"
    )


def _build_file_list_root(_m: re.Match[str], _text: str) -> RoutedCall | None:
    """Variant for rules that have no "directory" named group
    (e.g. 'list files', 'what files are there'). Kept separate so a
    missing group raises in tests instead of being swallowed by
    classify_for_direct_dispatch's defensive catch-all."""
    return RoutedCall("file_list", {}, rule_id="file_list.root")


def _build_file_search(m: re.Match[str], _text: str) -> RoutedCall | None:
    query = _validate_arg(m.group("query"))
    if not query:
        return None
    return RoutedCall("file_search", {"query": query}, rule_id="file_search.basic")


# Ordered list. First match wins, so put more specific patterns first.
# All regexes are anchored at start-of-string so a long prose query that
# happens to contain "search my memory for" mid-sentence does NOT match.
_RULES: list[_Rule] = [
    # memory_context — "what's in my memory?" / "memory overview"
    (
        "memory_context.basic",
        re.compile(
            r"^\s*(?:what(?:'s| is) in (?:my )?memory|"
            r"(?:show|list|give) (?:me )?(?:my )?memory (?:overview|context|summary))\s*\??\s*$",
            re.IGNORECASE,
        ),
        _build_memory_context,
    ),
    # memory_search — "search my memory for X" / "find in memory X"
    (
        "memory_search.basic",
        re.compile(
            r"^\s*(?:search (?:my |the )?memory (?:for|about) |"
            r"find (?:in (?:my |the )?memory|memory about) |"
            r"recall )(?P<query>.{1,200}?)\s*\??\s*$",
            re.IGNORECASE,
        ),
        _build_memory_search,
    ),
    # file_read — "read FILE" / "open FILE" / "show me FILE" / "cat FILE"
    # Path heuristic: contains a dot or a slash (e.g. README.md, src/main.py).
    # A bare word like "show me the bug" is NOT a file path.
    (
        "file_read.basic",
        re.compile(
            r"^\s*(?:read|open|cat|show me|display) "
            r"(?:the (?:file |contents of )?)?"
            r"(?P<path>[\w./-]*[./][\w./-]+)"
            r"\s*\??\s*$",
            re.IGNORECASE,
        ),
        _build_file_read,
    ),
    # file_list — "list files in DIR" / "list dir DIR" / "ls DIR"
    (
        "file_list.directory",
        re.compile(
            r"^\s*(?:list (?:files|contents|dir(?:ectory)?) (?:in|of) |"
            r"ls |dir )"
            r"(?P<directory>[\w./-]+)"
            r"\s*\??\s*$",
            re.IGNORECASE,
        ),
        _build_file_list_with_directory,
    ),
    # file_list — "list files" with no directory → workspace root
    (
        "file_list.root",
        re.compile(
            r"^\s*(?:list (?:all )?(?:the )?files|"
            r"what files (?:are there|exist|are in (?:the |this )?(?:workspace|project|repo)))"
            r"\s*\??\s*$",
            re.IGNORECASE,
        ),
        _build_file_list_root,
    ),
    # file_search — "grep QUERY" / "search for QUERY in files"
    (
        "file_search.basic",
        re.compile(
            r"^\s*(?:grep (?:for )?|"
            r"search (?:the )?(?:files|code|codebase) (?:for|about) |"
            r"find (?:in (?:files|code)) )"
            r"(?P<query>.{1,200}?)\s*\??\s*$",
            re.IGNORECASE,
        ),
        _build_file_search,
    ),
]


def classify_for_direct_dispatch(text: str) -> RoutedCall | None:
    """Pure regex classifier. Returns a RoutedCall on high-confidence match.

    None on:
      - empty / whitespace-only input
      - no rule matched
      - rule matched but arg extraction failed validation
    """
    if not text or not text.strip():
        return None
    for rule_id, pattern, builder in _RULES:
        m = pattern.match(text)
        if not m:
            continue
        try:
            call = builder(m, text)
        except Exception as e:  # pragma: no cover — defensive
            logger.debug("router rule %s raised %s, falling through", rule_id, e)
            return None
        if call is None:
            logger.debug("router rule %s matched but rejected args, fallthrough", rule_id)
            return None
        logger.debug("router rule %s → %s(%s)", rule_id, call.tool_name, call.args)
        return call
    return None


# ── Graph node ──────────────────────────────────────────────────────────


def _find_latest_user_text(messages: list[BaseMessage]) -> str | None:
    """Find the most recent HumanMessage content.

    SKIPS 🪞-prefixed HumanMessages (C1 critique markers) — those are
    never end-user input and must not be regex-classified.
    """
    for msg in reversed(messages):
        if not isinstance(msg, HumanMessage):
            continue
        content = msg.content if isinstance(msg.content, str) else str(msg.content)
        if content.startswith("🪞"):
            continue
        return content
    return None


def make_intent_router_node(tools: list[BaseTool]):
    """Factory: creates the intent_router graph node.

    Closure captures the list of tool names that are actually bound to
    the agent. This is the second safety gate: even if a regex matches,
    we won't dispatch a tool the agent doesn't have. Misconfigured
    agents (e.g. one without memory tools) won't crash on a "search my
    memory" prompt — they'll just fall through to the LLM.
    """
    # Resolve tool name → bound BaseTool once at build time.
    tool_by_name = {t.name: t for t in tools if t.name in READ_ONLY_TOOL_ALLOWLIST}

    if not tool_by_name:
        logger.info(
            "🚦 intent_router: NO allowlisted read-only tools bound; "
            "router will always fall through to LLM"
        )

    async def intent_router(state: Any) -> dict[str, Any]:
        # Feature flag check — read live setting each call so we can
        # toggle without restarting the agent.
        from app.config import settings as live_settings
        if not getattr(live_settings, "direct_tool_routing_enabled", False):
            return {}

        # Don't route mid-conversation. The router is a fast-path for
        # the FIRST hop of a turn; any later turn means the LLM is
        # already engaged and the user might be following up on tool
        # results. Heuristic: if the last message is anything other
        # than a HumanMessage, we're mid-flow → fallthrough.
        messages = list(state.messages)
        if not messages or not isinstance(messages[-1], HumanMessage):
            return {}

        # Don't route on critique markers (C1).
        text = _find_latest_user_text(messages)
        if not text:
            return {}
        last_content = messages[-1].content if isinstance(messages[-1].content, str) else str(messages[-1].content)
        if last_content.startswith("🪞"):
            return {}

        call = classify_for_direct_dispatch(text)
        if call is None:
            return {}

        # Allowlist gate (defensive — classify_for_direct_dispatch only
        # returns allowlisted names today, but enforce here too).
        if call.tool_name not in READ_ONLY_TOOL_ALLOWLIST:
            logger.warning(
                "🚦 router rejected non-allowlisted tool %s (rule=%s)",
                call.tool_name, call.rule_id,
            )
            return {}

        # Tool-availability gate.
        if call.tool_name not in tool_by_name:
            logger.debug(
                "🚦 router matched %s but tool not bound to agent; fallthrough",
                call.tool_name,
            )
            return {}

        # Synthesize an AIMessage with a tool call. The ToolNode
        # downstream will execute it; then we route to call_llm so the
        # actor sees the result and decides what to do next.
        tool_call_id = f"router_{uuid.uuid4().hex[:12]}"
        synthetic = AIMessage(
            content="",
            tool_calls=[{
                "id": tool_call_id,
                "name": call.tool_name,
                "args": dict(call.args),  # defensive copy
            }],
            # Mark for observability so we can spot router-originated
            # calls in traces and the dashboard.
            additional_kwargs={"router_dispatched": True, "router_rule": call.rule_id},
        )
        logger.info(
            "🚦 router → %s(%s) [rule=%s, tool_call_id=%s]",
            call.tool_name, call.args, call.rule_id, tool_call_id,
        )
        return {"messages": [synthetic]}

    return intent_router


def route_after_intent_router(state: Any) -> str:
    """Conditional edge after intent_router.

    Returns:
        "tools"    if the router dispatched a synthetic AIMessage with tool_calls
        "call_llm" otherwise (fallthrough — normal LLM-driven flow)
    """
    messages = list(state.messages)
    if not messages:
        return "call_llm"
    last = messages[-1]
    if isinstance(last, AIMessage) and last.tool_calls:
        # Only treat as router-dispatched if WE made it. Other AIMessages
        # at the tail would be from a resumed session and we should not
        # re-execute their tools here.
        if last.additional_kwargs.get("router_dispatched"):
            return "tools"
    return "call_llm"
