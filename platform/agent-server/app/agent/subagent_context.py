"""SubagentContext — per-request fleet governance state.

This module is the single source of truth for "can this agent spawn another
agent right now?" decisions. It is NOT about *how* to spawn — that's
``app.agent.subagent``. It's about the budget envelope and depth fence that
keep the fleet from melting credit cards.

Why a ContextVar (not a parameter):
    LangGraph + LangChain pass tool kwargs through several layers we don't
    own (StructuredTool wrappers, the truncation wrapper from graph.py,
    the executor's thread pool). Threading an extra parameter through all
    of them would be invasive and easy to drop on the floor — and dropping
    it on the floor is the difference between "subagent enforces budget"
    and "subagent silently spends $50". A ContextVar is propagated by
    asyncio/anyio automatically across `await` boundaries within the same
    request, which is exactly the scope we want.

Why a soft deadline (not a hard kill):
    LangGraph doesn't expose a clean cancellation API mid-stream. Instead
    we record the deadline once at the root, and `spawn_subagent` checks
    it before AND after each child invocation. A child that has already
    started runs to completion (or until its own internal step-limit fires),
    but no new child will be started past the deadline.

Why depth, calls_remaining, and tokens_remaining all live together:
    They're a unified failure domain. If any of them is exhausted, no more
    subagent calls — full stop. Splitting them into separate guard objects
    invites the bug where you check three out of four and forget the fourth.

Defaults:
    A request that NEVER touches `init_root_context()` (e.g., a tool unit
    test) gets a permissive default context — depth=0, ample budget — so
    the test doesn't have to set anything up just to exercise the wrapper.
    Production initializes via init_root_context() in chat.py.
"""
from __future__ import annotations

import logging
import time
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from typing import Optional


logger = logging.getLogger(__name__)


# ── Tunables: hard ceilings the system will NEVER cross ────────────────────
# These are intentionally conservative. They're meant to catch runaway
# recursion / runaway spending, not to be hit in normal operation.
#
# Pick numbers that make a misconfigured agent loud (it'll hit the ceiling
# and fail) rather than expensive (it'll silently spend $50). It's much
# easier to relax a ceiling later than to claw back tokens already spent.

MAX_DEPTH_CEILING = 3
"""Absolute hard cap on subagent nesting depth.

depth=0 → root user request (no subagent yet)
depth=1 → root spawned a child (one level of subagent)
depth=2 → a child spawned a grandchild
depth=3 → grandchild spawned a great-grandchild — REJECTED

Why 3 and not 5? Subagent answer quality decays super-linearly with depth
because each layer summarizes context for the next (telephone effect).
Past depth=3 the responses are usually confident garbage that nobody can
trace. If you find yourself wanting depth=4 the right move is usually to
flatten the design (map-reduce one wide level, not three narrow ones).
"""

MAX_FANOUT_CEILING = 8
"""Max number of subagents a single parent can spawn during one request.

Independent of depth — this caps WIDTH at any single level. A misconfigured
loop ("for f in 100 files: spawn_subagent(...)") will fail loud at the 9th
call instead of forking 100 LLM calls and discovering the OOM later.
"""

DEFAULT_BUDGET_TOKENS = 60_000
"""Default total token budget shared across ALL subagents in this request.

Roughly enough for 3-5 substantial child invocations on a frontier model
or 8-10 on a local 7B. Each child decrements `tokens_remaining` by its
actual usage; once <=0, no more spawns.

Tokens, not dollars, because dollars depend on which model the child
picks and we don't want this layer doing pricing math. Cost attribution
happens downstream in AuditAspect using the actual model used.
"""

DEFAULT_DEADLINE_MS = 120_000
"""Default wallclock budget for the whole fleet from root request start.

2 minutes is generous for an IDE-style request, conservative for a batch
job. Production callers override via init_root_context(deadline_ms=...).
"""


# ── State ─────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class SubagentContext:
    """Immutable per-request fleet state.

    Immutability is deliberate: mutating shared state across asyncio tasks
    is the bug factory we just escaped by using a ContextVar in the first
    place. To "decrement" budget on a child spawn, build a new context and
    set it on the ContextVar for the child's scope only (see
    ``derive_child_context``).
    """

    # Identity & lineage — for audit log threading
    root_session_id: str
    """The original chat session_id from the user's request. Stays constant
    no matter how deeply we recurse. Lets the audit log group an entire
    fleet by root request."""

    parent_session_id: str
    """The session_id of the IMMEDIATE parent. At depth=0 this equals
    root_session_id. At depth>0 it's the spawner's session id. Required
    by the AuditAspect on the Java side to draw the parent→child edge."""

    depth: int = 0
    """Recursion depth. depth=0 means "this is the root request", a
    spawn at depth=0 produces a child running at depth=1, etc."""

    # Budget envelope — all decrement as the fleet runs
    fanout_used: int = 0
    """How many subagents THIS context (not the whole tree) has spawned
    so far. Reset to 0 on each derive_child_context() call because the
    child gets its own fresh fanout budget."""

    tokens_remaining: int = DEFAULT_BUDGET_TOKENS
    """Shared token budget across the WHOLE fleet rooted at root_session_id.
    Unlike fanout, this is decremented monotonically and carried into
    children — so a greedy grandchild can starve its uncles."""

    deadline_unix_ms: int = 0
    """Absolute wallclock deadline as unix milliseconds (NOT a duration).
    Using an absolute time means children inherit "you have until T" not
    "you have 30s" — so a child that's already burned 25s of the parent's
    deadline only gets 5s left, not a fresh 30s."""

    # Tool gate
    allowed_tools: frozenset[str] = field(default_factory=frozenset)
    """Tool name allowlist for direct children. Empty set means "no tools" —
    deliberately strict default; the parent MUST opt children into specific
    tools. A child cannot expand its allowlist (enforced in derive_child).

    Stored as frozenset so the dataclass stays hashable + immutable."""

    # Cost attribution — reset per child to track per-leaf costs
    tokens_used_self: int = 0
    """Tokens this individual subagent consumed (for per-leaf reporting).
    NOT the same as DEFAULT_BUDGET_TOKENS - tokens_remaining (which is a
    fleet-wide accumulator)."""

    def remaining_ms(self) -> int:
        """Wallclock budget left from NOW until the absolute deadline."""
        if self.deadline_unix_ms <= 0:
            return DEFAULT_DEADLINE_MS  # no deadline set → treat as fresh budget
        return max(0, self.deadline_unix_ms - int(time.time() * 1000))


# Module-level ContextVar. The default is a permissive "nothing has been
# initialized yet" context — see _make_default_context. Production paths
# overwrite it via init_root_context() in chat.py.
subagent_context: ContextVar[SubagentContext] = ContextVar(
    "subagent_context",
    default=None,  # type: ignore[arg-type]
)


def _make_default_context() -> SubagentContext:
    """Build a permissive context used when nothing was initialized.

    Used by unit tests and by code paths that call spawn_subagent without
    going through the FastAPI chat endpoint. The intent is: tests don't
    need to set this up just to exercise wrapper logic, but production
    requests MUST initialize via init_root_context (chat.py does this).
    """
    return SubagentContext(
        root_session_id="adhoc",
        parent_session_id="adhoc",
        depth=0,
        fanout_used=0,
        tokens_remaining=DEFAULT_BUDGET_TOKENS,
        deadline_unix_ms=int(time.time() * 1000) + DEFAULT_DEADLINE_MS,
        allowed_tools=frozenset(),
    )


def get_context() -> SubagentContext:
    """Return the current SubagentContext, creating a default if unset.

    Callers should treat the result as read-only — to *change* the context
    for a scoped child invocation, use ``derive_child_context`` + the
    ContextVar's set/reset pattern.
    """
    ctx = subagent_context.get()
    if ctx is None:
        # No init in this call chain. Materialize the permissive default
        # but DON'T store it back — we want each unit-test invocation to
        # get a fresh default, not a leaked one from another test.
        return _make_default_context()
    return ctx


# ── Root context init (called by chat.py per-request) ──────────────────────
def init_root_context(
    *,
    root_session_id: str,
    allowed_tools: Optional[list[str]] = None,
    token_budget: int = DEFAULT_BUDGET_TOKENS,
    deadline_ms: int = DEFAULT_DEADLINE_MS,
) -> SubagentContext:
    """Install a root SubagentContext at the start of a user request.

    Must be called BEFORE the agent starts streaming events. The returned
    context is also set on the ContextVar so all downstream tool invocations
    in this request see it.

    Args:
        root_session_id: The user's chat session_id. Used as both
            root_session_id and parent_session_id at depth=0.
        allowed_tools: Tool names the ROOT agent is permitted to delegate
            via spawn_subagent. The root itself uses its full tool list
            from the agent config — this allowlist is only for children.
            Empty/None means "no subagent spawning allowed at root level"
            (which is the safe default for agents that don't opt in).
        token_budget: Initial shared token budget for the whole fleet.
        deadline_ms: Wallclock budget in milliseconds from NOW. Converted
            to an absolute deadline so children inherit time pressure.

    Returns:
        The root SubagentContext (also stored on the ContextVar).
    """
    ctx = SubagentContext(
        root_session_id=root_session_id,
        parent_session_id=root_session_id,
        depth=0,
        fanout_used=0,
        tokens_remaining=token_budget,
        deadline_unix_ms=int(time.time() * 1000) + deadline_ms,
        allowed_tools=frozenset(allowed_tools or []),
    )
    subagent_context.set(ctx)
    return ctx


# ── Spawn-time derivation: parent → child context ──────────────────────────
class SpawnRejected(RuntimeError):
    """Raised when a spawn_subagent call would violate the fleet envelope.

    The message MUST be human-readable because the LLM will see it as a
    tool error and decide what to do next ("retry with smaller scope?",
    "give up gracefully?"). Don't leak stack traces or internal IDs.
    """


def derive_child_context(
    parent: SubagentContext,
    *,
    child_session_id: str,
    requested_tools: list[str],
    estimated_tokens: int,
) -> SubagentContext:
    """Build the SubagentContext a child will run under.

    This is where all spawn-time policy lives. The function either returns
    a properly-narrowed child context or raises SpawnRejected.

    Policies enforced (each maps to one branch below):
        1. Depth: child must not exceed MAX_DEPTH_CEILING
        2. Fanout: parent must not exceed MAX_FANOUT_CEILING children
        3. Token budget: must have enough tokens left for the estimate
        4. Wallclock: must have positive remaining time
        5. Tool allowlist: child can only request a SUBSET of parent's
           allowed_tools (monotonic narrowing — never broadening)

    Args:
        parent: The current context (from get_context()).
        child_session_id: Session id assigned to the child.
        requested_tools: Tools the parent wants the child to have.
        estimated_tokens: Optimistic estimate of how many tokens the
            child might consume. Decremented from the fleet budget on
            spawn (refunded by actual usage when the child returns).

    Returns:
        A new SubagentContext with depth+1 and narrowed allowlist.

    Raises:
        SpawnRejected: any policy violation, with an actionable message.
    """
    # Policy 1: depth ceiling
    if parent.depth + 1 > MAX_DEPTH_CEILING:
        raise SpawnRejected(
            f"depth limit exceeded: parent at depth={parent.depth}, "
            f"max nesting is {MAX_DEPTH_CEILING}. Restructure the work "
            f"to avoid deeper recursion (e.g., flatten to map-reduce at "
            f"one level instead of multiple nested calls)."
        )

    # Policy 2: fanout ceiling
    if parent.fanout_used + 1 > MAX_FANOUT_CEILING:
        raise SpawnRejected(
            f"fanout limit exceeded: parent has already spawned "
            f"{parent.fanout_used} children at this level, max is "
            f"{MAX_FANOUT_CEILING}. Batch the work into fewer, larger "
            f"subagent calls."
        )

    # Policy 3: token budget
    if estimated_tokens > parent.tokens_remaining:
        raise SpawnRejected(
            f"token budget exhausted: {parent.tokens_remaining} tokens "
            f"left in this request's fleet budget, but child estimated "
            f"to need {estimated_tokens}. The root request is out of "
            f"runway — answer directly instead of spawning."
        )

    # Policy 4: wallclock
    if parent.remaining_ms() <= 0:
        raise SpawnRejected(
            "wallclock deadline exceeded for this request — cannot spawn "
            "more subagents. Return what you have."
        )

    # Policy 5: tool allowlist must be a SUBSET of parent's
    requested = set(requested_tools)
    if parent.allowed_tools:
        # If parent has an allowlist, enforce monotonic narrowing.
        # (At depth=0 with no init, allowed_tools is empty, which means
        # no subagent spawning is allowed at all — handled below.)
        disallowed = requested - parent.allowed_tools
        if disallowed:
            raise SpawnRejected(
                f"requested tools {sorted(disallowed)} are not in the "
                f"parent's allowlist {sorted(parent.allowed_tools)}. "
                f"A subagent can never have MORE permissions than its "
                f"parent — request only tools the parent itself can use."
            )
    else:
        # Parent has no allowlist → spawning entirely forbidden in this
        # request. This is the safe default for agents that haven't
        # opted into fleet mode via init_root_context(allowed_tools=...).
        raise SpawnRejected(
            "subagent spawning is not enabled for this request. The "
            "parent agent must declare allowed_tools at request init "
            "to permit delegation."
        )

    return SubagentContext(
        root_session_id=parent.root_session_id,
        parent_session_id=parent.parent_session_id,  # see note below
        depth=parent.depth + 1,
        fanout_used=0,  # child gets its own fresh fanout budget
        tokens_remaining=parent.tokens_remaining - estimated_tokens,
        deadline_unix_ms=parent.deadline_unix_ms,  # inherit absolute deadline
        # Child inherits the SAME allowlist (it can request a subset of
        # its own at the next spawn; we narrow lazily there, not here).
        allowed_tools=parent.allowed_tools,
        tokens_used_self=0,
    )
    # NOTE: parent_session_id passed to the child below is the IMMEDIATE
    # parent's session id, not necessarily the root's. The caller of
    # derive_child_context is responsible for updating it before installing
    # on the ContextVar — see spawn_subagent in subagent.py.


def record_fanout(parent: SubagentContext) -> SubagentContext:
    """Return a new parent context with fanout_used incremented by 1.

    Called after a successful spawn so the parent's NEXT spawn attempt
    sees the updated count. Kept separate from derive_child_context
    because the parent's context and the child's context live on the
    ContextVar at different times (parent's is restored when the child
    returns), so we must rewrite the parent's stored copy explicitly.
    """
    return replace(parent, fanout_used=parent.fanout_used + 1)


def record_consumption(parent: SubagentContext, tokens_used: int) -> SubagentContext:
    """Return a new parent context with the actual tokens used by the
    child subtracted from the fleet budget.

    Two-phase accounting: derive_child_context() pessimistically reserves
    `estimated_tokens` upfront so concurrent spawns can't all simultaneously
    pass the "we have budget" check. After the child returns we settle up
    using its ACTUAL usage. The net effect is each spawn's budget impact =
    actual usage, but mid-flight we err on the side of refusing extra
    spawns when concurrent children might overspend.

    Args:
        parent: The parent context to update.
        tokens_used: Actual token usage reported by the child.

    Returns:
        New parent context with tokens_remaining adjusted.
    """
    # We previously subtracted `estimated_tokens` in derive_child_context.
    # The caller knows what estimate was used; for simplicity we just
    # subtract the difference here (actual - estimate). If the child used
    # MORE than estimated we deduct the extra; less than estimated, we
    # refund.
    # The caller passes the SIGNED delta (positive = additional debit,
    # negative = refund) as tokens_used. See subagent.py:_settle_budget.
    return replace(
        parent,
        tokens_remaining=max(0, parent.tokens_remaining - tokens_used),
    )
