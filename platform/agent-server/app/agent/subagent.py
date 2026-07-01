"""spawn_subagent — invoke a scoped subagent in-process.

This is the engine behind the `spawn_subagent` LangChain tool. The tool
itself (defined in app.tools.subagent_tool) is a thin schema wrapper; all
the interesting work lives here so unit tests can drive it without going
through the LangChain BaseTool machinery.

Design choice: invoke the child agent IN-PROCESS, not over HTTP.

    Rationale:
    1. Auth: spawning over HTTP means minting another JWT, going through
       auth-service, paying network latency every time. In-process means
       the child inherits the parent's tenant_context directly (which is
       what we want — children are NOT separate principals from the
       parent's perspective).
    2. Streaming: langgraph's astream_events() is async-native. Calling
       it in-process lets us await the child to completion (or partial
       result) and capture the tool trace cleanly. Over HTTP we'd have
       to re-parse SSE markers we already had typed.
    3. Cancellation: in-process we can in principle cancel via asyncio
       Task cancellation; over HTTP we'd need a separate /v1/abort
       endpoint that doesn't exist yet.

    The HTTP path (which CAN be useful for distributed fleets that span
    machines) is intentionally NOT built here. It belongs in a separate
    `app.agent.remote_subagent` once we actually need cross-host fleets.
    Today everything runs on one box.

Design choice: build an EPHEMERAL agent per spawn, don't reuse cached agents.

    The cached agents in graph.py are keyed by (model_name) and share a
    checkpointer + tool list. A subagent needs a NARROWED tool list (the
    whole point of subagent isolation), so we can't reuse the cache. We
    DO reuse the underlying chat model object — `_create_chat_model` is
    cheap and doesn't open connections — but we build a fresh ReAct agent
    with the subset tools each spawn.

    Cost: ~1ms to assemble the graph. Worth it for guaranteed isolation.

Design choice: capture the child's tool trace from astream_events.

    The parent agent will see ONLY the child's final answer + a 1-line
    summary of how many tools the child used. We don't return the full
    tool trace to the parent because:
    - It's verbose (4-10 tool calls × ~500 chars each = 5KB context bloat)
    - The parent isn't supposed to micro-manage child decisions
    - The trace IS captured and logged for the audit log / dashboard /
      eval harness (which is what cares about the detail)
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

from langchain_core.messages import HumanMessage, SystemMessage

from app.agent.fleet_bus import is_cancelled, publish_event
from app.agent.subagent_context import (
    SpawnRejected,
    SubagentContext,
    derive_child_context,
    get_context,
    record_consumption,
    record_fanout,
    subagent_context,
)
from app.usage import estimate_tokens

logger = logging.getLogger(__name__)


# ── Per-spawn result envelope ──────────────────────────────────────────────
@dataclass
class SubagentResult:
    """What a single spawn produced — opaque to the LLM, structured for logs.

    The LLM that called spawn_subagent only sees ``format_for_llm()``;
    everything else here is for observability (AuditAspect, dashboard,
    eval harness) and tests.
    """

    child_session_id: str
    role: str
    """Free-form role label the parent passed in (e.g. "sql-specialist").
    Used for log grouping and the dashboard fleet view. NOT enforced
    semantically — it's a hint, not an identity."""

    answer: str
    """The child agent's final natural-language answer. May be empty if
    the child crashed; ``error`` will be set in that case."""

    tool_names: list[str] = field(default_factory=list)
    """Tools the child actually called, in order. Used by the eval
    harness to verify subagent trajectories."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    duration_ms: int = 0

    error: Optional[str] = None
    """Set if the spawn failed mid-flight (LLM exception, deadline reached,
    tool error escaped, etc.). When set, ``answer`` is usually empty and
    the LLM should treat the spawn as failed."""

    depth: int = 0
    """Depth at which the child ran (parent.depth + 1). Used by the audit
    log to draw the tree."""

    # ── C3 verifier fields ─────────────────────────────────────────────
    verified: Optional[bool] = None
    """Verifier outcome — None when verifier didn't run (disabled, crashed,
    unparseable), True when the answer passed grading, False when it
    failed and was either auto-retried unsuccessfully or shipped with
    a ⚠️ marker. None is DELIBERATELY DIFFERENT from False so callers
    and dashboards can distinguish 'no signal' from 'verifier rejected'."""

    verifier_grade: Optional[int] = None
    """Numeric 1-5 grade from the verifier, or None if not graded."""

    verifier_reasoning: str = ""
    """One-line explanation from the verifier. Empty if not graded."""

    verifier_retried: bool = False
    """True if the auto-retry path fired (regardless of whether the
    retry's grade was better). False otherwise."""

    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def format_for_llm(self) -> str:
        """Render the result as a single string the parent LLM will see.

        Kept terse on purpose. The parent should be reading the answer
        for content and the metadata line for "did this go well?" — NOT
        re-reading every tool the child called.
        """
        if self.error:
            return (
                f"❌ subagent [{self.role}] failed after "
                f"{self.duration_ms}ms: {self.error}\n"
                f"(no answer produced)"
            )
        tool_summary = (
            ", ".join(self.tool_names) if self.tool_names else "no tools"
        )
        return (
            f"✅ subagent [{self.role}] finished in "
            f"{self.duration_ms}ms ({self.total_tokens()} tokens, "
            f"used: {tool_summary})\n\n"
            f"--- subagent answer ---\n{self.answer}"
        )


# ── The spawn primitive ────────────────────────────────────────────────────
async def spawn_subagent(
    *,
    role: str,
    brief: str,
    allowed_tools: list[str],
    model: Optional[str] = None,
    max_tool_calls: int = 10,
    max_tokens: int = 8000,
) -> SubagentResult:
    """Spawn a scoped subagent to answer ``brief`` and return its result.

    This is the function the LangChain tool wrapper calls. Tests should
    call this directly — the LangChain tool wrapper adds nothing beyond
    schema validation.

    Args:
        role: Short label for logs/dashboard ("sql-specialist", "reviewer").
        brief: The full task description for the child. This BECOMES the
            child's first user message; treat it as a complete prompt.
        allowed_tools: Tool name allowlist for the child. Must be a SUBSET
            of the parent's allowed_tools (enforced in
            derive_child_context). Empty list means "no tools, pure LLM
            reasoning" — useful for delegated reasoning tasks.
        model: Override the child's model. Defaults to the same model as
            the parent agent.
        max_tool_calls: Internal step limit for the child's ReAct loop.
            Defends against a child that gets stuck in a tool loop.
        max_tokens: Caller's TOKEN estimate (used for upfront budget
            reservation). The actual usage will be settled at completion.

    Returns:
        A SubagentResult — never raises on the happy path. On policy
        rejection, returns a SubagentResult with ``error`` set so the
        parent LLM can see the failure and decide what to do.
    """
    parent_ctx = get_context()
    start_ms = int(time.time() * 1000)
    child_session_id = f"{parent_ctx.root_session_id}/sub-{uuid.uuid4().hex[:8]}"

    # ── Phase 1: gate-keep at the budget envelope ─────────────────────────
    # derive_child_context is where ALL policy lives — depth, fanout, token
    # budget, deadline, tool subset. If any of those fail we synthesize a
    # result object instead of raising, because the caller (a LangChain
    # tool) is going to feed this back to the LLM as text. The LLM should
    # see a clean structured error, not a Python stack trace.
    try:
        child_ctx = derive_child_context(
            parent_ctx,
            child_session_id=child_session_id,
            requested_tools=allowed_tools,
            estimated_tokens=max_tokens,
        )
        # The immediate parent (for audit log lineage) is the spawner, NOT
        # the root. Override the inherited parent_session_id here.
        child_ctx = _with_parent_session(child_ctx, parent_ctx.root_session_id
                                         if parent_ctx.depth == 0
                                         else parent_ctx.parent_session_id)
    except SpawnRejected as e:
        logger.warning(
            "❌ spawn_subagent rejected (role=%s, depth=%d): %s",
            role, parent_ctx.depth, e,
        )
        return SubagentResult(
            child_session_id=child_session_id,
            role=role,
            answer="",
            error=str(e),
            depth=parent_ctx.depth + 1,
            duration_ms=int(time.time() * 1000) - start_ms,
        )

    # Record the fanout immediately so two concurrent spawns can't both
    # squeak past the MAX_FANOUT check by racing.
    subagent_context.set(record_fanout(parent_ctx))

    logger.info(
        "🤖 spawn_subagent role=%s depth=%d session=%s tools=%s budget=%d",
        role, child_ctx.depth, child_session_id,
        allowed_tools, child_ctx.tokens_remaining,
    )

    # ── Phase 2: build the child agent with the narrowed tool set ──────────
    try:
        child_agent = _build_child_agent(
            allowed_tools=allowed_tools,
            model_name=model,
            max_tool_calls=max_tool_calls,
            role=role,
        )
    except Exception as e:
        # Build failures are configuration bugs (unknown tool, missing
        # model). Surface them as a SubagentResult so the LLM can fall
        # back gracefully.
        logger.exception("spawn_subagent build failed for role=%s", role)
        # Refund the upfront budget since the child never actually ran.
        subagent_context.set(record_consumption(get_context(), -max_tokens))
        return SubagentResult(
            child_session_id=child_session_id,
            role=role,
            answer="",
            error=f"failed to construct subagent: {e}",
            depth=child_ctx.depth,
            duration_ms=int(time.time() * 1000) - start_ms,
        )

    # ── Phase 3: invoke the child under its narrowed context ───────────────
    # Install child_ctx for the duration of the child's invocation; restore
    # the parent's (updated) context afterwards. The fanout bump from above
    # already lives in the parent's context, so when we restore it the
    # next spawn attempt will see fanout_used+1 correctly.
    parent_token = subagent_context.set(child_ctx)
    result: Optional[SubagentResult] = None
    try:
        result = await _run_child(
            agent=child_agent,
            brief=brief,
            child_session_id=child_session_id,
            role=role,
            depth=child_ctx.depth,
            deadline_ms=child_ctx.remaining_ms(),
            root_session_id=parent_ctx.root_session_id,
        )

        # ── C3 verifier gate ──────────────────────────────────────────
        # Only verify successful runs. A child that crashed or timed
        # out already has `error` set; no answer to grade.
        if result.error is None:
            await _maybe_verify_and_retry(
                result=result,
                original_brief=brief,
                child_agent=child_agent,
                child_session_id=child_session_id,
                role=role,
                child_ctx=child_ctx,
                root_session_id=parent_ctx.root_session_id,
            )
            _publish_verified_event(
                root_session_id=parent_ctx.root_session_id,
                child_session_id=child_session_id,
                role=role,
                result=result,
            )
    finally:
        # Restore the parent's view, then debit it by ACTUAL usage.
        #
        # Why not also refund the upfront `max_tokens` reservation?
        # Because we never debited the parent's view by it in the first
        # place. The reservation flows DOWNWARD only: derive_child_context
        # produces a child with `tokens_remaining = parent - reservation`,
        # so any grandchildren see the reduced budget. But the PARENT's
        # `tokens_remaining` is never touched by the reservation — only
        # by the final settlement.
        #
        # Net effect: parent's budget shrinks by actual_used (correct),
        # children/grandchildren are protected against parent overspend
        # (via the conservative downstream reservation).
        actual_used = result.total_tokens() if result is not None else max_tokens
        subagent_context.reset(parent_token)
        subagent_context.set(record_consumption(get_context(), actual_used))

    result.duration_ms = int(time.time() * 1000) - start_ms
    return result


# ── Internals ──────────────────────────────────────────────────────────────
async def _maybe_verify_and_retry(
    *,
    result: SubagentResult,
    original_brief: str,
    child_agent,
    child_session_id: str,
    role: str,
    child_ctx: SubagentContext,
    root_session_id: str,
) -> None:
    """C3 — verify the child's answer; optionally auto-retry once.

    Mutates ``result`` IN PLACE because callers already hold the
    reference and we want budget settlement + duration calculation to
    use the final state. Mutation is bounded to the C3 fields plus
    ``answer`` (when we mark with ⚠️ or replace with a retry answer)
    and accumulating ``prompt_tokens`` / ``completion_tokens`` /
    ``tool_names`` on retry.

    Fail-open: verifier failures leave ``result.verified=None`` and
    the original answer is untouched. The whole subagent return path
    after this function MUST work identically to a verifier-disabled
    deployment when the verifier crashes — that's the contract.
    """
    # Lazy import to keep module load cheap for the rejection-only
    # test path that never actually constructs a verifier.
    from app.agent.subagent_verifier import (
        format_retry_brief,
        format_verifier_marker,
        verify_subagent_answer,
    )
    from app.config import settings

    if not settings.subagent_verifier_enabled:
        return

    # First-pass grade.
    verdict = await verify_subagent_answer(
        brief=original_brief,
        answer=result.answer,
    )

    # passed=None → verifier disabled / crashed / unparseable.
    # Leave result.verified=None (the field default) so downstream
    # can distinguish "no signal" from "verifier rejected".
    if verdict.passed is None:
        result.verifier_reasoning = verdict.reasoning  # carry the skip reason
        # Don't publish child_verified for skipped runs — subscribers
        # treat the absence of the event as "no verifier signal".
        return

    # Record the first verdict's metadata regardless of pass/fail.
    result.verified = verdict.passed
    result.verifier_grade = verdict.grade
    result.verifier_reasoning = verdict.reasoning

    if verdict.passed:
        return  # happy path — no marker, no retry

    # First-pass failed. Decide between auto-retry and ship-with-marker.
    if not settings.subagent_verifier_auto_retry:
        # Mark the answer and ship as-is. Parent LLM will see the ⚠️
        # marker and can decide how to use the answer.
        result.answer = format_verifier_marker(verdict, result.answer)
        return

    # Auto-retry: run the child ONE more time with the verifier's
    # reasoning prepended to the brief.
    logger.info(
        "⚠️ verifier failed grade=%d, retrying subagent role=%s",
        verdict.grade, role,
    )
    retry_brief = format_retry_brief(original_brief, verdict)

    # The retry runs under the SAME child_session_id deliberately —
    # subscribers tracking this child see a "second wind" rather than
    # a new sibling. The deadline is whatever budget remains; a retry
    # past deadline degrades to "ship the original with marker".
    remaining_ms = child_ctx.remaining_ms()
    if remaining_ms < 1000:  # <1s left → don't bother
        logger.info(
            "⚠️ verifier retry skipped: only %dms remaining of deadline",
            remaining_ms,
        )
        result.answer = format_verifier_marker(verdict, result.answer)
        result.verifier_retried = False
        return

    retry_result = await _run_child(
        agent=child_agent,
        brief=retry_brief,
        child_session_id=child_session_id,
        role=role,
        depth=result.depth,
        deadline_ms=remaining_ms,
        root_session_id=root_session_id,
    )
    result.verifier_retried = True

    # Accumulate retry usage into the original result so budget
    # settlement covers the full spawn (both attempts).
    result.prompt_tokens += retry_result.prompt_tokens
    result.completion_tokens += retry_result.completion_tokens
    result.tool_names = list(result.tool_names) + list(retry_result.tool_names)

    if retry_result.error is not None:
        # Retry crashed; keep the original answer + ⚠️ marker. The
        # retry's verifier_grade was the cause of the failure that
        # triggered the retry — preserve it for observability.
        result.answer = format_verifier_marker(verdict, result.answer)
        return

    # Re-verify the retry attempt. If it passes, ship it. If it
    # fails again, ship it WITH the second verdict's marker.
    second_verdict = await verify_subagent_answer(
        brief=original_brief,
        answer=retry_result.answer,
    )
    if second_verdict.passed is True:
        result.answer = retry_result.answer
        result.verified = True
        result.verifier_grade = second_verdict.grade
        result.verifier_reasoning = (
            f"retry passed ({second_verdict.grade}/5): "
            f"{second_verdict.reasoning}"
        )
        return

    # Second verdict failed or was inconclusive. Ship the retry
    # answer with a marker — it's at least the most recent attempt
    # to follow the verifier's guidance. Use whichever verdict has
    # a definitive grade for the marker.
    marker_verdict = second_verdict if second_verdict.passed is False else verdict
    result.answer = format_verifier_marker(marker_verdict, retry_result.answer)
    if second_verdict.grade is not None:
        result.verifier_grade = second_verdict.grade
        result.verifier_reasoning = (
            f"retry failed ({second_verdict.grade}/5): "
            f"{second_verdict.reasoning}"
        )
    # result.verified stays False from the first verdict — second
    # attempt didn't recover.


def _publish_verified_event(
    *,
    root_session_id: str,
    child_session_id: str,
    role: str,
    result: SubagentResult,
) -> None:
    """Fire-and-forget child_verified event on the fleet bus.

    Pulled out as a helper so _maybe_verify_and_retry's many exit
    paths can each call it once. The bus publish is a no-op when
    no subscribers are registered for the session (e.g. unit tests
    or non-streaming callers) — see fleet_bus._publish_to_session.
    """
    if not root_session_id:
        return
    if result.verified is None:
        # Verifier didn't produce a usable verdict. Dashboard treats
        # absence of the event as "no signal" — see EVENT_TYPES doc.
        return
    publish_event(
        root_session_id=root_session_id,
        child_session_id=child_session_id,
        role=role,
        event_type="child_verified",
        verified=result.verified,
        grade=result.verifier_grade,
        reasoning=result.verifier_reasoning,
        retried=result.verifier_retried,
    )


def _with_parent_session(ctx: SubagentContext, parent_session_id: str) -> SubagentContext:
    """Return a new context with parent_session_id set.

    Tiny helper because dataclasses are frozen — we need replace() and
    we use this exactly once, but the intent is clearer with a named fn.
    """
    from dataclasses import replace

    return replace(ctx, parent_session_id=parent_session_id)


def _build_child_agent(
    *,
    allowed_tools: list[str],
    model_name: Optional[str],
    max_tool_calls: int,
    role: str,
):
    """Construct an ephemeral langgraph agent with the narrowed tool set.

    NOT cached — each spawn gets a fresh agent. See module docstring for
    why caching would be unsafe here (different spawns request different
    tool subsets, even for the same role label).

    The child uses an in-memory checkpointer scoped to this single spawn:
    the child IS the entire conversation, it has no history before its
    brief and no future after its final answer. No need to share state
    with the parent.
    """
    # Local imports avoid the heavy graph.py module loading on cold tests
    # that don't actually invoke the agent (e.g., pure rejection tests).
    from langgraph.checkpoint.memory import MemorySaver
    from langgraph.prebuilt import create_react_agent

    from app.agent.graph import _create_chat_model, _truncate_tool_output
    from app.config import settings
    from app.registry.tool_registry import resolve_tools

    # Resolve the tools the parent requested. resolve_tools enforces the
    # registry check (UnknownToolError if the parent asked for a tool that
    # doesn't exist). We deliberately use strict=True here regardless of
    # the env var — a subagent with a silently-shrunken tool set is even
    # worse than the parent agent, which at least the user is watching.
    resolved = resolve_tools(allowed_tools, strict=True) if allowed_tools else []

    # Reuse the parent's truncation wrapper for output limiting.
    max_output = getattr(settings, "max_tool_output", 4000)
    wrapped = [_truncate_tool_output(t, max_output) for t in resolved]

    model = _create_chat_model(model_name)

    role_prompt = _build_subagent_system_prompt(role=role, max_tool_calls=max_tool_calls)

    # Ephemeral checkpointer — child conversation doesn't outlive the spawn.
    return create_react_agent(
        model=model,
        tools=wrapped,
        prompt=lambda state: [SystemMessage(content=role_prompt)] + state["messages"],
        checkpointer=MemorySaver(),
    )


def _build_subagent_system_prompt(*, role: str, max_tool_calls: int) -> str:
    """The system prompt the child agent runs under.

    Deliberately different from the parent's main system prompt:
    - much SHORTER (the child has one specific task; doesn't need
      the full IDE-agent backstory)
    - constrains the child to its role, no scope creep
    - forbids the child from spawning *more* subagents inside itself
      (depth control happens at the policy layer too, but a prompt
      reminder helps the LLM not waste a tool-call trying)
    - tells the child to return a SHORT direct answer because the
      parent is going to read it programmatically, not as conversation
    - FORBIDS paraphrasing identifiers extracted from tool output.
      Telephone-effect mitigation: if the parent asked the child to
      find a class name / constant / function name, the child must
      echo the EXACT bytes from the source — not an "improved" version.
      This requirement is documented as the v4-eval finding in
      eval-harness commit 30dee58 (children turned MAX_DEPTH_CEILING
      into MAX_SUBAGENT_NESTING_DEPTH, SubagentResult into
      SpawnResultEnvelope — plausible but wrong).
    """
    return (
        f"You are a specialist subagent acting as: {role}.\n"
        "\n"
        "Your task is given in the user message below. Complete it using\n"
        "the tools you have, then return a SHORT direct answer (1-3\n"
        "paragraphs). Your answer is consumed by another agent, not a\n"
        "human — be terse and information-dense, no pleasantries.\n"
        "\n"
        f"RULES:\n"
        f"- Hard limit: {max_tool_calls} tool calls. Plan accordingly.\n"
        "- DO NOT spawn more subagents. You are a leaf worker.\n"
        "- DO NOT ask clarifying questions — make your best inference\n"
        "  from the task description and proceed.\n"
        "- If the task is impossible or out of scope, say so in one\n"
        "  sentence and explain why.\n"
        "\n"
        "VERBATIM QUOTING (CRITICAL — your output is consumed by code, not a human):\n"
        "- If the task asks for an identifier (class name, function name,\n"
        "  constant, variable, decorator, type alias), QUOTE IT VERBATIM\n"
        "  from the tool output. Copy the exact bytes; do not normalize\n"
        "  capitalization; do not 'improve' the name; do not expand or\n"
        "  contract abbreviations. `MAX_DEPTH_CEILING` is NOT `MAX_SUBAGENT_NESTING_DEPTH`.\n"
        "- If the task asks for a literal phrase, copy the bytes between\n"
        "  the quotes / markers in the source. Do not summarize.\n"
        "- If you cannot find the requested identifier in the file(s) you\n"
        "  read, say so explicitly: 'identifier X not found in <file>'.\n"
        "  DO NOT invent a plausible-looking replacement.\n"
        "- For prose/concept questions (not identifier extraction) a\n"
        "  natural-language summary is fine — these rules apply only when\n"
        "  the parent asked for a name/string lookup.\n"
    )


async def _run_child(
    *,
    agent,
    brief: str,
    child_session_id: str,
    role: str,
    depth: int,
    deadline_ms: int,
    root_session_id: str = "",
) -> SubagentResult:
    """Drive the child's astream_events loop, capturing answer + trace.

    Mirrors the structure of chat.py's _stream_agent_response but produces
    a single result object instead of SSE chunks. The two implementations
    diverge in: this one uses a tight deadline, doesn't emit Kafka events,
    doesn't write to ConversationStore, and aggregates tokens locally.

    Fleet streaming (P1 #5):
        If ``root_session_id`` is set AND a fleet_bus is registered for
        that session, this function publishes ``child_start``,
        ``child_token``, ``child_tool_start``, ``child_tool_end``,
        ``child_end`` / ``child_cancelled`` events to the bus. Subscribers
        (chat.py's SSE stream, dashboard, etc.) receive a real-time view
        of the child's progress.

        Cancel is cooperative: between LangGraph events we poll
        ``fleet_bus.is_cancelled``. If a cancel was requested we break
        out of the stream and return a partial SubagentResult with
        ``error="cancelled by parent"``. We do NOT interrupt the LLM
        mid-token — that would require LangGraph cancel-token support
        we don't have yet. The typical case is: the parent decides early
        between tool rounds, where cancel is cheap and snappy.

    Backward compat:
        ``root_session_id`` defaults to empty so callers (notably unit
        tests) that don't set it still work. Publishes to an empty
        session id are silent no-ops via the bus's "unknown session →
        drop" rule.
    """
    config = {"configurable": {"thread_id": child_session_id}}
    answer_parts: list[str] = []
    tool_names: list[str] = []
    prompt_tokens = 0
    completion_tokens = 0
    cancelled = False

    # Announce the child's existence so subscribers can render a row
    # before any tokens arrive. brief_preview is bounded so we don't
    # flood the wire if the parent passed a giant brief.
    if root_session_id:
        publish_event(
            root_session_id=root_session_id,
            child_session_id=child_session_id,
            role=role,
            event_type="child_start",
            depth=depth,
            brief_preview=brief[:200],
        )

    # Wrap the entire stream in a wallclock deadline using asyncio.wait_for.
    # If the deadline trips we still return a partial result with whatever
    # the child managed to produce so far.
    async def _drive() -> tuple[str, list[str], int, int, bool]:
        nonlocal prompt_tokens, completion_tokens
        local_answer: list[str] = []
        local_tools: list[str] = []
        was_cancelled = False
        async for event in agent.astream_events(
            {"messages": [HumanMessage(content=brief)]},
            config=config,
            version="v2",
        ):
            # Cooperative cancellation point — checked between events so
            # an in-flight tool call completes naturally (tool cancellation
            # is a separate, unsolved problem documented in the module
            # docstring). For typical fan-out cancel scenarios the parent
            # decides between tool rounds, where this hook is sufficient.
            if root_session_id and is_cancelled(
                root_session_id=root_session_id,
                child_session_id=child_session_id,
            ):
                was_cancelled = True
                break

            kind = event.get("event", "")
            if kind == "on_chat_model_start":
                # Same accounting trick as chat.py — sum input message
                # contents for a real prompt_tokens estimate (rather than
                # just counting the brief).
                data = event.get("data", {}).get("input", {})
                msgs = data.get("messages", []) if isinstance(data, dict) else []
                if msgs and isinstance(msgs[0], list):
                    msgs = msgs[0]
                for m in msgs:
                    c = getattr(m, "content", None)
                    if isinstance(c, str):
                        prompt_tokens += estimate_tokens(c)
            elif kind == "on_chat_model_stream":
                chunk = event.get("data", {}).get("chunk")
                if chunk and getattr(chunk, "content", None):
                    local_answer.append(chunk.content)
                    completion_tokens += estimate_tokens(chunk.content)
                    if root_session_id:
                        publish_event(
                            root_session_id=root_session_id,
                            child_session_id=child_session_id,
                            role=role,
                            event_type="child_token",
                            token=chunk.content,
                        )
            elif kind == "on_tool_start":
                tool_name = event.get("name", "unknown")
                local_tools.append(tool_name)
                if root_session_id:
                    tool_input = event.get("data", {}).get("input", {})
                    publish_event(
                        root_session_id=root_session_id,
                        child_session_id=child_session_id,
                        role=role,
                        event_type="child_tool_start",
                        tool=tool_name,
                        input_preview=str(tool_input)[:200],
                    )
            elif kind == "on_tool_end":
                if root_session_id:
                    publish_event(
                        root_session_id=root_session_id,
                        child_session_id=child_session_id,
                        role=role,
                        event_type="child_tool_end",
                        tool=event.get("name", "unknown"),
                        output_preview=str(event.get("data", {}).get("output", ""))[:200],
                    )
        return (
            "".join(local_answer),
            local_tools,
            prompt_tokens,
            completion_tokens,
            was_cancelled,
        )

    try:
        # Convert ms to seconds for asyncio. Floor at 1s — if the deadline
        # is already <1s we still want to give the child a chance to fail
        # loud rather than time-out immediately and produce nothing.
        timeout_s = max(1.0, deadline_ms / 1000.0)
        answer, tool_names, p, c, cancelled = await asyncio.wait_for(
            _drive(), timeout=timeout_s
        )
        result = SubagentResult(
            child_session_id=child_session_id,
            role=role,
            answer=answer.strip(),
            tool_names=tool_names,
            prompt_tokens=p,
            completion_tokens=c,
            depth=depth,
            error="cancelled by parent" if cancelled else None,
        )
        if root_session_id:
            publish_event(
                root_session_id=root_session_id,
                child_session_id=child_session_id,
                role=role,
                event_type="child_cancelled" if cancelled else "child_end",
                answer_preview=result.answer[:200],
                tokens=result.total_tokens(),
                tool_names=tool_names,
                error=result.error,
            )
        return result
    except asyncio.TimeoutError:
        result = SubagentResult(
            child_session_id=child_session_id,
            role=role,
            answer="".join(answer_parts).strip(),
            tool_names=tool_names,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            depth=depth,
            error=f"subagent exceeded wallclock deadline of {deadline_ms}ms",
        )
        if root_session_id:
            publish_event(
                root_session_id=root_session_id,
                child_session_id=child_session_id,
                role=role,
                event_type="child_end",
                answer_preview="",
                tokens=result.total_tokens(),
                tool_names=tool_names,
                error=result.error,
            )
        return result
    except Exception as e:
        logger.exception("subagent role=%s crashed", role)
        result = SubagentResult(
            child_session_id=child_session_id,
            role=role,
            answer="",
            tool_names=tool_names,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            depth=depth,
            error=f"subagent crashed: {type(e).__name__}: {e}",
        )
        if root_session_id:
            publish_event(
                root_session_id=root_session_id,
                child_session_id=child_session_id,
                role=role,
                event_type="child_end",
                answer_preview="",
                tokens=result.total_tokens(),
                tool_names=tool_names,
                error=result.error,
            )
        return result
