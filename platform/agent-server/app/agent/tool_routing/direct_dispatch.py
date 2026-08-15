"""Conservative direct-dispatch classification for obvious read-only queries.

When the user's message is clearly a one-tool read-only operation
(e.g. "search my memory for X", "list files in /tmp", "read README.md"),
we can skip the first call_llm hop and dispatch the tool call directly.
The LLM still sees the tool result on the next hop and decides whether
to continue.

WHY this design (operational rationale):
========================================

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

MEASUREMENT NOTE:
    Measure direct-routing rate, latency, and agreement on the labeled eval set
    before enabling this feature. Never auto-route writes or execution tools.

"""
from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

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
        except Exception as e:  # noqa: BLE001 - policy classifier fails closed
            logger.debug("router rule %s raised %s, falling through", rule_id, e)
            return None
        if call is None:
            logger.debug("router rule %s matched but rejected args, fallthrough", rule_id)
            return None
        logger.debug("router rule %s → %s(%s)", rule_id, call.tool_name, call.args)
        return call
    return None
