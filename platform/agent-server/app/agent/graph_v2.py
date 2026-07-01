"""LangGraph ReAct agent v2 — explicit StateGraph replacing naive create_react_agent wrapper.

Fixes from the v1 code review:
1. Token Leak: Compression now uses message IDs so add_messages reducer overwrites in-place
2. Sync I/O in modifier: Workspace context fetched asynchronously in a dedicated graph node
3. Brittle error recovery: Structured error tracking via state field, not string matching
4. MemorySaver leak: Production checkpointer with Redis/Postgres fallback (kept from v1)
5. Flat message list: Separated conversation_history from active_observations via state schema

Architecture:
  inject_context → call_llm → route → execute_tools → compress_history → call_llm → ...
                                  ↘ END (if no tool calls or max loops)
"""

import asyncio
import copy
import functools
import logging
from typing import Annotated, Any, Literal, Sequence

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.tools import BaseTool
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode
from pydantic import BaseModel, Field

from app.agent.prompts import get_system_prompt
from app.agent.reflexion import make_maybe_critique_node
from app.agent.tool_router import (
    make_intent_router_node,
    route_after_intent_router,
)
from app.agent.compressor import make_context_compressor_node
from app.config import settings

logger = logging.getLogger(__name__)


# ── State Definition ──────────────────────────────────────────


class AgentState(BaseModel):
    """Explicit agent state — separates concerns the v1 flat list conflated.

    conversation_history: The actual message graph (User + AI + Tool messages).
        Managed by LangGraph's add_messages reducer which deduplicates by message ID.

    workspace_context: Injected once per session, not re-read every LLM call.
        Avoids the sync I/O smell from v1.

    loop_counter: Tracks ReAct iterations to enforce max_steps.
        Replaces the opaque recursion_limit from create_react_agent.

    consecutive_errors: Typed error tracking instead of string-matching "error" in content.
        Resets to 0 on any successful tool execution.
    """

    messages: Annotated[Sequence[BaseMessage], add_messages] = Field(
        default_factory=list
    )
    workspace_context: str = ""
    skills_catalog: str = ""
    loop_counter: int = 0
    consecutive_errors: int = 0
    # ── Reflexion / self-critique (C1) ──────────────────────────────────
    # Number of revision passes the critic has triggered for this request.
    # SEPARATE from loop_counter — critique retries and tool-use rounds
    # are different failure domains and must be capped independently.
    # When >= settings.reflexion_max_attempts, the critic short-circuits
    # to no-op (see app/agent/reflexion.py).
    critique_attempts: int = 0
    # ── Context compression (Phase 4) ──────────────────────────────────
    # Structured investigation state — survives compression, provides
    # ground truth of what the agent has confirmed/eliminated.
    investigation_summary: str = ""
    # Summary of what was dropped during last compression (for debugging).
    compression_summary: str = ""


# ── Graph Nodes ───────────────────────────────────────────────


async def inject_context_node(state: AgentState) -> dict[str, Any]:
    """Async workspace + skills-catalog injection — runs once at the start of a request.

    Two cheap fetches in parallel:
      - workspace_context: blocking FS read offloaded to a thread.
      - skills_catalog:    network call to the memory server, capped + cached.

    Both are best-effort: failure leaves the field empty and the agent runs without it.
    """
    needs_workspace = not state.workspace_context
    needs_skills = not state.skills_catalog

    if not needs_workspace and not needs_skills:
        logger.debug("⏭️  Context already injected, skipping")
        return {}

    async def _load_workspace() -> str:
        try:
            from app.context.workspace import get_workspace_context
            from app.tools.agent_mode import get_workspace_root

            root = get_workspace_root()
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(None, get_workspace_context, root)
        except Exception as e:
            logger.debug("Workspace context unavailable: %s", e)
            return ""

    async def _load_skills() -> str:
        try:
            from app.agent.skills_index import build_skill_catalog_block
            from app.auth.middleware import tenant_context

            tenant_id = tenant_context.get() or "default"
            return await build_skill_catalog_block(tenant_id)
        except Exception as e:
            logger.debug("Skills catalog unavailable: %s", e)
            return ""

    tasks = []
    if needs_workspace:
        tasks.append(_load_workspace())
    if needs_skills:
        tasks.append(_load_skills())

    results = await asyncio.gather(*tasks, return_exceptions=False)
    out: dict[str, Any] = {}

    idx = 0
    if needs_workspace:
        ws_ctx = results[idx]
        idx += 1
        logger.info("📂 Workspace context injected (%d chars)", len(ws_ctx))
        out["workspace_context"] = ws_ctx
    if needs_skills:
        skills = results[idx]
        if skills:
            logger.info("🧠 Skills catalog injected (%d chars)", len(skills))
        out["skills_catalog"] = skills

    return out


def _build_llm_messages(state: AgentState, system_prompt: str) -> list[BaseMessage]:
    """Build the message list for the LLM call.

    Applies context window budget ONLY to what the LLM sees — does NOT
    mutate the persisted state. This is the "view" pattern from v1,
    but extracted into a pure function for testability.
    """
    messages = list(state.messages)

    # Prepend system prompt with workspace context + skills catalog.
    # ORDER MATTERS: skills catalog goes AFTER the base prompt but BEFORE workspace
    # so the LLM sees the contract ("here are tools") before specific knowledge
    # ("here are skills you can pull on-demand") before raw repo facts.
    full_prompt = system_prompt
    if state.skills_catalog:
        full_prompt += "\n\n" + state.skills_catalog
    if state.workspace_context:
        full_prompt += "\n\nWORKSPACE:\n" + state.workspace_context

    # Compress old tool messages (keep last 4 uncompressed)
    tool_indices = [i for i, m in enumerate(messages) if isinstance(m, ToolMessage)]
    if len(tool_indices) > 4:
        old_indices = set(tool_indices[:-4])
        for i in old_indices:
            msg = messages[i]
            if isinstance(msg, ToolMessage):
                content = msg.content if isinstance(msg.content, str) else str(msg.content)
                if len(content) > 1500:
                    from app.agent.graph import _smart_compress

                    compressed = _smart_compress(content, 1500, tool_name=msg.name or "")
                    messages[i] = ToolMessage(
                        id=msg.id,  # Preserve ID for add_messages dedup
                        content=compressed,
                        tool_call_id=msg.tool_call_id,
                        name=msg.name,
                    )

    # Enforce character budget — drop oldest messages if over budget
    max_chars = settings.max_context_chars
    total = sum(
        len(m.content) if isinstance(m.content, str) else len(str(m.content))
        for m in messages
    )
    if total > max_chars:
        kept = []
        running = 0
        for msg in reversed(messages):
            msg_len = (
                len(msg.content)
                if isinstance(msg.content, str)
                else len(str(msg.content))
            )
            if running + msg_len > max_chars:
                break
            kept.insert(0, msg)
            running += msg_len
        messages = kept

    return [SystemMessage(content=full_prompt)] + messages


def make_call_llm_node(model: BaseChatModel, tools: list[BaseTool], system_prompt: str):
    """Factory: creates the LLM call node with bound tools.

    The LLM node builds an optimized message view (compression + budget),
    calls the model, and increments the loop counter.
    """
    # Bind tools once — this converts our tool schemas to the provider's format
    model_with_tools = model.bind_tools(tools) if tools else model

    async def call_llm_node(state: AgentState) -> dict[str, Any]:
        messages = _build_llm_messages(state, system_prompt)
        msg_count = len(messages)
        total_chars = sum(len(m.content) if isinstance(m.content, str) else len(str(m.content)) for m in messages)
        logger.info(
            "🧠 LLM call [loop=%d, msgs=%d, chars=%d, errors=%d]",
            state.loop_counter + 1, msg_count, total_chars, state.consecutive_errors,
        )
        logger.debug(
            "🧠 LLM message types: %s",
            [type(m).__name__ for m in messages],
        )

        import time
        t0 = time.monotonic()
        response = await model_with_tools.ainvoke(messages)
        elapsed_ms = int((time.monotonic() - t0) * 1000)

        # Log what the LLM decided
        if hasattr(response, "tool_calls") and response.tool_calls:
            tool_names = [tc.get("name", "?") for tc in response.tool_calls]
            logger.info(
                "🤖 LLM decided to call %d tool(s): %s [%dms]",
                len(tool_names), tool_names, elapsed_ms,
            )
            for tc in response.tool_calls:
                args_summary = {k: (str(v)[:80] + "..." if len(str(v)) > 80 else v) for k, v in tc.get("args", {}).items()}
                logger.debug("   🔧 %s(%s)", tc.get("name"), args_summary)
        else:
            content_preview = (response.content[:120] + "...") if len(response.content) > 120 else response.content
            logger.info(
                "🤖 LLM responded with text [%dms, %d chars]: %s",
                elapsed_ms, len(response.content), content_preview,
            )

        return {
            "messages": [response],
            "loop_counter": state.loop_counter + 1,
        }

    return call_llm_node


def make_route_node(max_steps: int):
    """Factory: creates the routing function that decides next step.

    Fixes v1 smell: error detection via string matching.
    Now uses the typed consecutive_errors state field.

    Reflexion integration (C1): when the upstream ``maybe_critique`` node
    appends a 🪞 HumanMessage to request a revision, we need to loop
    back to ``call_llm`` so the actor produces a revised answer. The
    plain "no tool_calls → END" rule from v2 base would END instead
    (the last message is a HumanMessage, not an AIMessage with tool
    calls). We detect the 🪞 marker on the last HumanMessage and route
    to ``call_llm`` in that case. The 🪞 prefix is enforced as a
    contract by ``reflexion.py``.
    """

    import re

    # Pattern to detect hallucinated tool use in plain text output.
    # Small models sometimes output "Using file_write..." or "```sh\nfile_write path=..."
    # instead of actually issuing a structured tool call.
    _HALLUCINATED_TOOL_RE = re.compile(
        r"(?:Using |🔧 Using |📝 |🔍 )?(file_write|file_read|file_list|file_edit|file_search|"
        r"code_run|code_shell|git_commit|git_diff|git_status|run_tests|"
        r"memory_set|memory_search|memory_context|rag_search)"
        r"[\s.:(]",
        re.IGNORECASE,
    )

    def route(state: AgentState) -> Literal["tools", "call_llm", "hallucination_fix", "__end__"]:
        # Max steps guard
        if state.loop_counter >= max_steps:
            logger.warning("🛑 Agent hit max steps (%d/%d), forcing end", state.loop_counter, max_steps)
            return "__end__"

        # Too many consecutive errors — stop to avoid infinite loop
        if state.consecutive_errors > 3:
            logger.warning("🛑 Agent hit max consecutive errors (%d), forcing end", state.consecutive_errors)
            return "__end__"

        last = state.messages[-1] if state.messages else None

        # If the LLM produced tool calls, execute them
        if isinstance(last, AIMessage) and last.tool_calls:
            tool_names = [tc.get("name", "?") for tc in last.tool_calls]
            logger.debug("➡️  Routing to tools: %s", tool_names)
            return "tools"

        # ── Hallucination guard ────────────────────────────────────────
        # Detect when the model DESCRIBES tool usage in plain text rather
        # than issuing actual tool_calls. This is a common failure mode
        # for small models (7B). We inject a correction and re-call the LLM.
        # BUT: skip this check if the previous message was a ToolMessage —
        # in that case the model is legitimately summarizing a real tool result.
        if isinstance(last, AIMessage) and not last.tool_calls:
            # Check if the message before this AIMessage was a tool result
            prev = state.messages[-2] if len(state.messages) >= 2 else None
            has_recent_tool_result = isinstance(prev, ToolMessage)
            if not has_recent_tool_result:
                content = last.content if isinstance(last.content, str) else str(last.content)
                matches = _HALLUCINATED_TOOL_RE.findall(content)
                if matches and state.loop_counter < max_steps - 1:
                    logger.warning(
                        "🚨 Hallucinated tool use detected (mentioned %s in text, no actual tool_calls). "
                        "Injecting correction [loop=%d].",
                        matches[:3], state.loop_counter,
                    )
                    return "hallucination_fix"  # Inject correction then re-call LLM

        # Reflexion: the critic injected a revision request as a 🪞
        # HumanMessage — loop back to call_llm so the actor revises.
        # We deliberately check the marker rather than "is HumanMessage"
        # because a checkpointer-resumed session might legitimately have
        # a HumanMessage tail from a previous user turn that we should
        # NOT auto-respond to here.
        if isinstance(last, HumanMessage):
            content = last.content if isinstance(last.content, str) else str(last.content)
            if content.startswith("🪞"):
                logger.debug("➡️  Routing to call_llm (critique revision pending)")
                return "call_llm"

        # No tool calls = LLM is done reasoning
        logger.debug("➡️  Routing to END (no tool calls)")
        return "__end__"

    return route


def make_error_tracking_node():
    """Wraps tool execution to track errors structurally.

    Fixes v1 smell: instead of checking `"error" in str(content)`,
    we check if the ToolMessage has `status="error"` (LangGraph native)
    or if it's an exception wrapper.
    """

    def track_errors(state: AgentState) -> dict[str, Any]:
        """Post-tool-execution: update error counter based on tool results."""
        recent_tool_msgs = []
        for msg in reversed(state.messages):
            if isinstance(msg, ToolMessage):
                recent_tool_msgs.append(msg)
            elif isinstance(msg, AIMessage):
                break

        # Log tool results
        for msg in recent_tool_msgs:
            content = msg.content if isinstance(msg.content, str) else str(msg.content)
            preview = (content[:150] + "...") if len(content) > 150 else content
            status_icon = "✅" if not content.startswith("❌") and getattr(msg, "status", None) != "error" else "❌"
            logger.info(
                "   %s Tool result [%s, %d chars]: %s",
                status_icon, msg.name or "?", len(content), preview.replace("\n", " "),
            )

        has_error = False
        for msg in recent_tool_msgs:
            if getattr(msg, "status", None) == "error":
                has_error = True
                break
            content = msg.content if isinstance(msg.content, str) else str(msg.content)
            if content.startswith("❌"):
                has_error = True
                break

        if has_error:
            new_count = state.consecutive_errors + 1
            logger.warning(
                "⚠️  Tool error detected (consecutive: %d/3)", new_count,
            )
            if new_count <= 3:
                hint = (
                    "⚠️ The previous tool call returned an error. "
                    "Review the error and try a different approach. "
                    f"(attempt {new_count}/3)"
                )
                return {
                    "consecutive_errors": new_count,
                    "messages": [HumanMessage(content=hint)],
                }
            return {"consecutive_errors": new_count}
        else:
            if state.consecutive_errors > 0:
                logger.debug("✅ Error streak reset (was %d)", state.consecutive_errors)
            return {"consecutive_errors": 0}

    return track_errors


def make_hallucination_fix_node():
    """Inject a correction message when the model hallucinated tool usage in text.

    This tells the model to actually USE the tools via function calling
    rather than describing what it would do in prose.
    """

    _CORRECTION = (
        "🚨 STOP. You just described using tools in plain text, but you did NOT "
        "actually call any tools. Your response was NOT executed. "
        "You MUST use the function calling interface to invoke tools — "
        "do NOT write tool names or commands in your text response. "
        "Try again: call the appropriate tool NOW using a proper function call."
    )

    def fix_hallucination(state: AgentState) -> dict[str, Any]:
        return {"messages": [HumanMessage(content=_CORRECTION)]}

    return fix_hallucination


def make_compress_history_node():
    """Explicit compression node that updates messages by ID.

    Fixes v1 token leak: by returning ToolMessages with the SAME id,
    the add_messages reducer overwrites them in the checkpoint store
    instead of appending duplicates. This means compression is persisted,
    not just applied ephemerally.
    """

    def compress_history(state: AgentState) -> dict[str, Any]:
        messages = list(state.messages)
        tool_indices = [i for i, m in enumerate(messages) if isinstance(m, ToolMessage)]

        if len(tool_indices) <= 4:
            return {}

        old_indices = set(tool_indices[:-4])
        updated = []

        for i in old_indices:
            msg = messages[i]
            if not isinstance(msg, ToolMessage):
                continue
            content = msg.content if isinstance(msg.content, str) else str(msg.content)
            if len(content) <= 1500:
                continue

            from app.agent.graph import _smart_compress

            compressed = _smart_compress(content, 1500, tool_name=msg.name or "")
            logger.debug(
                "📦 Compressed tool output [%s]: %d → %d chars",
                msg.name or "?", len(content), len(compressed),
            )
            updated.append(
                ToolMessage(
                    id=msg.id,
                    content=compressed,
                    tool_call_id=msg.tool_call_id,
                    name=msg.name,
                )
            )

        if updated:
            logger.info("📦 Compressed %d old tool outputs", len(updated))
        return {"messages": updated} if updated else {}

    return compress_history


# ── Tool Output Truncation (kept from v1) ─────────────────────


def _truncate_tool_output(tool: BaseTool, max_chars: int) -> BaseTool:
    """Wrap a tool so its output is truncated to ``max_chars``.

    See ``graph._truncate_tool_output`` for the rationale: under
    langchain-core ≥1.4, monkey-patching ``_arun`` drops the ``config``
    kwarg. We route through ``StructuredTool.from_function`` + ``ainvoke``
    instead, which is the supported public API.
    """
    from langchain_core.tools import StructuredTool

    inner = tool

    def _truncate(result: Any) -> Any:
        if isinstance(result, str) and len(result) > max_chars:
            omitted = len(result) - max_chars
            return (
                result[:max_chars]
                + f"\n\n... (truncated, {omitted} chars omitted. "
                + "Use start_line/end_line for specific ranges)"
            )
        return result

    async def _wrapped_async(**kwargs):
        return _truncate(await inner.ainvoke(kwargs))

    def _wrapped_sync(**kwargs):
        return _truncate(inner.invoke(kwargs))

    return StructuredTool.from_function(
        func=_wrapped_sync,
        coroutine=_wrapped_async,
        name=inner.name,
        description=inner.description,
        args_schema=inner.args_schema,
        response_format=getattr(inner, "response_format", "content"),
    )


# ── Graph Builder ─────────────────────────────────────────────


def build_agent_graph(
    model: BaseChatModel,
    tools: list[BaseTool],
    system_prompt: str,
    checkpointer=None,
    max_steps: int | None = None,
):
    """Build the explicit StateGraph for the ReAct agent.

    Graph topology:
        START → inject_context → intent_router ──(dispatched)──→ tools → track_errors → compress → call_llm → ...
                                              └─(fallthrough)──→ call_llm → maybe_critique → route ──→ tools → track_errors → compress → call_llm
                                                                                                       └──→ END

    The ``intent_router`` node (C2) inspects the latest user message
    via regex and, when ``settings.direct_tool_routing_enabled`` is on
    AND the message is an unambiguous single-tool READ (e.g.
    "search my memory for X"), synthesizes the tool call directly,
    skipping the first LLM hop. Otherwise it's a no-op pass-through.
    See ``app/agent/tool_router.py`` for the allowlist and rules.

    The ``maybe_critique`` node (C1) is a no-op pass-through when
    reflexion is disabled (``settings.reflexion_enabled = False``).
    When enabled, if the LLM produced a final-answer turn (no tool
    calls), the critic grades it; below-threshold grades inject a
    revision HumanMessage so ``route`` then routes back to call_llm
    via the 🪞-marker branch.
    """
    max_steps = max_steps or settings.max_agent_steps

    # Truncate tool outputs (defense-in-depth, same as v1)
    max_output = settings.max_tool_output
    truncated_tools = [_truncate_tool_output(t, max_output) for t in tools]

    graph = StateGraph(AgentState)

    # Nodes
    graph.add_node("inject_context", inject_context_node)
    graph.add_node("intent_router", make_intent_router_node(truncated_tools))
    graph.add_node("call_llm", make_call_llm_node(model, truncated_tools, system_prompt))
    graph.add_node("maybe_critique", make_maybe_critique_node())
    graph.add_node("tools", ToolNode(truncated_tools))
    graph.add_node("track_errors", make_error_tracking_node())
    graph.add_node("compress_history", make_compress_history_node())
    graph.add_node("hallucination_fix", make_hallucination_fix_node())
    graph.add_node("context_compressor", make_context_compressor_node(
        budget_tokens=settings.max_context_chars // 4,  # chars → tokens
        threshold=0.75,
    ))

    # Edges
    graph.add_edge(START, "inject_context")
    graph.add_edge("inject_context", "intent_router")
    # intent_router → tools (if dispatched) OR call_llm (fallthrough).
    # The fallthrough path joins the existing call_llm → maybe_critique
    # → route loop, so reflexion still applies to any LLM-driven turn.
    # The dispatch path joins the existing tools → track_errors →
    # compress → call_llm loop, so the LLM sees the tool result on the
    # next hop and decides whether to continue.
    graph.add_conditional_edges(
        "intent_router",
        route_after_intent_router,
        {"tools": "tools", "call_llm": "call_llm"},
    )
    graph.add_edge("call_llm", "maybe_critique")
    graph.add_conditional_edges("maybe_critique", make_route_node(max_steps))
    graph.add_edge("hallucination_fix", "call_llm")
    graph.add_edge("tools", "track_errors")
    graph.add_edge("track_errors", "compress_history")
    graph.add_edge("compress_history", "context_compressor")
    graph.add_edge("context_compressor", "call_llm")

    return graph.compile(checkpointer=checkpointer)


# ── Public API (drop-in replacement for graph.py) ─────────────


def create_agent_v2(model_name: str | None = None, temperature: float | None = None):
    """Create a v2 agent with explicit StateGraph.

    `temperature` forwarded to `_create_chat_model`. None ⇒ default from settings.
    """
    from app.agent.graph import _create_chat_model, _get_checkpointer
    from app.tools.definitions import get_tools

    model = _create_chat_model(model_name, temperature=temperature)
    tools = get_tools()
    return build_agent_graph(
        model=model,
        tools=tools,
        system_prompt=get_system_prompt(),
        checkpointer=_get_checkpointer(),
    )


def create_agent_v2_from_config(config, temperature: float | None = None):
    """Create a v2 agent from a YAML AgentConfig.

    `temperature` forwarded to `_create_chat_model`. None ⇒ default from settings.
    """
    from app.agent.graph import _create_chat_model, _get_checkpointer
    from app.registry.tool_registry import resolve_tools

    model = _create_chat_model(config.model, temperature=temperature)
    tools = resolve_tools(config.tools)
    max_steps = config.guardrails.get("max_tool_calls", settings.max_agent_steps)
    prompt = config.prompt or get_system_prompt()

    return build_agent_graph(
        model=model,
        tools=tools,
        system_prompt=prompt,
        checkpointer=_get_checkpointer(),
        max_steps=max_steps,
    )
