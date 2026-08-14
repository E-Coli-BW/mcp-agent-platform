"""LangGraph ReAct agent — the brain of the coding agent.

This module is the most architecturally important file in the Python agent.
It ties together: LLM model selection, tool binding, context window management,
and the ReAct (Reasoning + Acting) agent loop.

Threading model:
- FastAPI runs on a single asyncio event loop (uvicorn)
- LangGraph's astream_events() is async — yields events on the same loop
- Tool execution: sync tools run in a thread pool (run_in_executor),
  async tools run directly on the event loop
- The state_modifier runs synchronously in the calling thread before each LLM call

Memory model:
- MemorySaver stores all messages in-memory (Python dict, keyed by thread_id)
- Each chat session gets its own thread_id → isolated conversation state
- state_modifier compresses this state BEFORE sending to LLM, but the
  full uncompressed state remains in MemorySaver for future turns
- This means RAM grows with conversation count, but each LLM call stays bounded
"""

from typing import TYPE_CHECKING

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import BaseTool
from langgraph.prebuilt import create_react_agent

from app.agent.prompts import get_system_prompt
from app.config import settings

if TYPE_CHECKING:
    from app.registry.config_loader import AgentConfig


def _smart_compress(content: str, max_chars: int, tool_name: str = "") -> str:
    """Context-aware compression based on tool type.

    Different tools produce different output formats:
    - file_read/rag_search → code: use AST-aware compression (signatures + docstrings)
    - file_list/file_search → listings: keep first 10 + last 10 lines
    - Default → head + tail split
    """
    if max_chars <= 0:
        return ""

    if tool_name in ("file_read", "rag_search"):
        from app.rag.compression.ast import compress_code_output

        return compress_code_output(content, max_chars, tool_name=tool_name)
    elif tool_name in ("file_list", "file_search"):
        lines = content.splitlines()
        if len(lines) > 20:
            return "\n".join(
                lines[:10]
                + [f"... ({len(lines) - 20} lines omitted) ..."]
                + lines[-10:]
            )
        return content
    if len(content) <= max_chars:
        return content
    half = max_chars // 2
    omitted = len(content) - max_chars
    return (
        content[:half]
        + f"\n...(truncated {omitted} chars)...\n"
        + content[-half:]
    )


def _summarize_tool_messages(messages: list, max_tool_chars: int = 1500) -> list:
    """Compress old tool messages to save context window.

    Uses _smart_compress for context-aware compression based on tool type:
    - file_read/rag_search → AST-aware (signatures + docstrings + returns)
    - file_list/file_search → first 10 + last 10 lines
    - Default → head + tail character split

    WHY TOOL-AWARE? Different tools produce fundamentally different output formats.
    Code benefits from AST compression (80% meaning in 20% text), while file listings
    are best served by keeping boundary entries. Generic output uses head+tail.
    """
    tool_indices = [i for i, m in enumerate(messages) if isinstance(m, ToolMessage)]

    if len(tool_indices) <= 4:
        return messages

    old_tool_indices = set(tool_indices[:-4])

    result = []
    for i, msg in enumerate(messages):
        if i in old_tool_indices and isinstance(msg, ToolMessage):
            content = msg.content if isinstance(msg.content, str) else str(msg.content)
            if len(content) > max_tool_chars:
                compressed = _smart_compress(
                    content, max_tool_chars, tool_name=msg.name or ""
                )
                result.append(
                    ToolMessage(
                        content=compressed,
                        tool_call_id=msg.tool_call_id,
                        name=msg.name,
                    )
                )
            else:
                result.append(msg)
        else:
            result.append(msg)
    return result


def _make_state_modifier(system_prompt: str):
    """Create a state modifier that trims messages to fit context window.

    WHAT IS A STATE MODIFIER?
    LangGraph calls this function BEFORE every LLM invocation. It receives
    the full conversation state and returns the messages to send to the LLM.
    The original state in MemorySaver is NOT modified — only the LLM sees
    the compressed version. This is like a "view" in database terms.

    EXECUTION TIMING:
    1. User sends message → appended to MemorySaver
    2. state_modifier runs (this function) → compresses for LLM
    3. LLM receives compressed messages → generates response
    4. Response appended to MemorySaver (full, uncompressed)
    5. If LLM called a tool → tool executes → result appended
    6. Go to step 2 (state_modifier runs again before next LLM call)

    RETRY MECHANISM:
    Small models (7B) sometimes produce malformed tool calls — e.g., outputting
    JSON that doesn't match the tool schema, or mixing text with tool calls.
    When this happens, the tool returns an error. We detect consecutive errors
    and append a corrective hint to help the LLM recover, up to 3 retries.

    MEMORY BUDGET:
    For a 7B model with 32K context window:
    - System prompt: ~500 tokens (~2K chars)
    - We budget ~5K tokens (~20K chars) for conversation history
    - Remaining ~26.5K tokens for LLM generation + tool definitions
    - Tool definitions (14 tools) consume ~3K tokens automatically
    """
    # Append workspace context to system prompt
    try:
        from app.context.workspace import get_workspace_context
        from app.tools.agent_mode import get_workspace_root

        ws_ctx = get_workspace_context(get_workspace_root())
        full_prompt = system_prompt + "\n\nWORKSPACE:\n" + ws_ctx
    except Exception:
        full_prompt = system_prompt

    RETRY_HINT = (
        "\n\n⚠️ Your previous response could not be parsed as a valid tool call. "
        "Please respond with EITHER a tool call OR a text response, not both. "
        "If calling a tool, use the exact function signature."
    )

    def modifier(state):
        # state["messages"] contains the FULL conversation history from MemorySaver
        # This includes: HumanMessage, AIMessage, ToolMessage, and AIMessageChunk
        messages = state["messages"]

        # ── Step 0: LLM Output Retry ──────────────────────────────
        # Check if the last message is a tool error (malformed LLM output)
        # This catches cases where the 7B model produces invalid JSON for tool calls
        if len(messages) >= 2:
            last = messages[-1]
            if (
                isinstance(last, ToolMessage)
                and last.content
                and "error" in str(last.content).lower()[:100]
            ):
                # Count how many consecutive errors we've had
                # (prevents infinite retry loops)
                error_count = 0
                for msg in reversed(messages):
                    if isinstance(msg, ToolMessage) and "error" in str(msg.content).lower()[:100]:
                        error_count += 1
                    else:
                        break
                if error_count <= 3:
                    # Append a HumanMessage with retry hint
                    # The LLM will see this and (hopefully) produce a valid response
                    # Note: list(messages) creates a shallow copy to avoid mutating MemorySaver
                    messages = list(messages) + [HumanMessage(content=RETRY_HINT)]

        # ── Step 1: Compress old tool outputs ─────────────────────
        # This is the main defense against context death spiral
        messages = _summarize_tool_messages(messages)

        # ── Step 2: Hard character budget ─────────────────────────
        # Even after tool compression, if conversation is very long,
        # drop the oldest messages to stay within budget
        # Configurable via AGENT_MAX_CONTEXT_CHARS env var
        max_chars = settings.max_context_chars
        total_chars = sum(
            len(m.content) if isinstance(m.content, str) else len(str(m.content))
            for m in messages
        )

        if total_chars > max_chars:
            # Iterate from newest to oldest, keeping messages until budget exhausted
            # This ensures the MOST RECENT messages (most relevant) are always kept
            kept = []
            running = 0
            for msg in reversed(messages):
                msg_len = (
                    len(msg.content)
                    if isinstance(msg.content, str)
                    else len(str(msg.content))
                )
                if running + msg_len > max_chars:
                    break  # Budget exhausted — drop remaining old messages
                kept.insert(0, msg)  # Prepend to maintain order
                running += msg_len
            messages = kept

        # ── Step 3: Prepend system prompt ─────────────────────────
        # System prompt is ALWAYS first — it defines the agent's behavior
        # LangGraph will pass this list directly to the LLM's chat API
        return [SystemMessage(content=full_prompt)] + messages

    return modifier


def _truncate_tool_output(tool: BaseTool, max_chars: int) -> BaseTool:
    """Wrap a tool so its output is truncated to ``max_chars``.

    IMPLEMENTATION NOTE (regression fix for langchain-core 1.4):
    -----------------------------------------------------------
    Previously this monkey-patched ``tool._arun`` with an
    ``async def truncated_arun(*args, **kwargs)`` wrapper. That broke under
    langchain-core ≥1.4 because ``BaseTool.ainvoke()`` *inspects* the
    callable's signature to decide which kwargs (notably ``config`` and
    ``run_manager``) to forward. A bare ``*args, **kwargs`` wrapper signals
    "I don't take config", so ``config`` is dropped — but the underlying
    ``StructuredTool._arun`` declares it keyword-only and raises:

        TypeError: StructuredTool._arun() missing 1 required
                   keyword-only argument: 'config'

    Fix: route through the supported public API. We construct a new
    ``StructuredTool`` whose ``coroutine`` calls ``tool.ainvoke(kwargs)``.
    ``ainvoke`` is the framework's documented entry point — it handles
    ``config`` defaulting and ``run_manager`` injection for us, so the
    wrapper code stays purely about truncation.
    """
    from langchain_core.tools import StructuredTool

    inner = tool  # captured by closure

    def _truncate(result):
        if isinstance(result, str) and len(result) > max_chars:
            omitted = len(result) - max_chars
            return (
                result[:max_chars]
                + "\n\n... (truncated, "
                + f"{omitted} chars omitted. "
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
        # Pass response_format through so e.g. content-and-artifact tools
        # still emit artifacts after truncation.
        response_format=getattr(inner, "response_format", "content"),
    )


def _create_chat_model(
    model_name: str | None = None,
    temperature: float | None = None,
) -> BaseChatModel:
    """Compatibility delegate to the model factory owner."""
    from app.agent.model.factory import create_chat_model

    return create_chat_model(model_name, temperature=temperature)


def _create_chat_model_with_fallback(
    model_name: str | None = None,
    temperature: float | None = None,
) -> BaseChatModel:
    """Compatibility delegate to the fallback model factory owner."""
    from app.agent.model.factory import create_chat_model_with_fallback

    return create_chat_model_with_fallback(model_name, temperature=temperature)


def _create_checkpointer():
    """Compatibility delegate to the LangGraph checkpointer owner."""
    from app.agent.checkpointing import create_checkpointer

    return create_checkpointer()


async def _ensure_async_checkpointer():
    """Compatibility delegate to async checkpointer initialization."""
    from app.agent.checkpointing import ensure_async_checkpointer

    return await ensure_async_checkpointer()


def _get_checkpointer():
    """Compatibility delegate to the LangGraph checkpointer owner."""
    from app.agent.checkpointing import get_checkpointer

    return get_checkpointer()


def __getattr__(name: str):
    if name in {"_checkpointer", "_async_checkpointer", "_async_checkpointer_cm"}:
        from app.agent import checkpointing

        return getattr(checkpointing, name)
    if name in {"_agents", "_agent_tool_names", "_agent_lock"}:
        from app.agent.engine.registry import get_agent_registry

        registry = get_agent_registry()
        return {
            "_agents": registry.agents,
            "_agent_tool_names": registry.tool_names,
            "_agent_lock": registry.lock,
        }[name]
    raise AttributeError(name)


def create_agent(model_name: str | None = None, temperature: float | None = None):
    """Create a LangGraph ReAct agent with all tools.

    WHAT create_react_agent DOES INTERNALLY:
    1. Takes the model and binds tools to it (model.bind_tools(tools))
    2. Creates a graph with two nodes: "agent" (LLM) and "tools" (executor)
    3. The agent node calls the LLM, which either:
       a. Returns a text response → graph ends
       b. Returns a tool_call → routes to tools node
    4. Tools node executes the tool, appends result as ToolMessage
    5. Routes back to agent node (LLM sees the tool result and decides next step)
    6. This loop continues until LLM returns text (no tool call) or max steps reached

    prompt: Called BEFORE each LLM invocation to compress the message
    history. The full history stays in MemorySaver; only the LLM sees compressed.
    (Replaces the deprecated state_modifier parameter in newer LangGraph versions.)

    checkpointer: Persists conversation state across API calls (same session_id
    = same conversation thread). Without this, each API call would start fresh.

    `temperature`: per-call override forwarded to `_create_chat_model`.
    None ⇒ use `settings.default_temperature`.
    """
    model = _create_chat_model_with_fallback(model_name, temperature=temperature)

    # Wrap every tool with output truncation (defense-in-depth)
    # Uses get_tools() for lazy re-probing of backend availability
    from app.tools.definitions import get_tools
    from app.runtime.tool_authorization import wrap_tool_with_authorization
    max_output = settings.max_tool_output
    truncated_tools = [_truncate_tool_output(t, max_output) for t in get_tools()]
    execution_tools = [wrap_tool_with_authorization(t) for t in truncated_tools]

    agent = create_react_agent(
        model=model,
        tools=execution_tools,
        prompt=_make_state_modifier(get_system_prompt()),
        checkpointer=_get_checkpointer(),
    )
    return agent


def create_agent_from_config(config: "AgentConfig", temperature: float | None = None):
    """Create a LangGraph agent from a declarative AgentConfig.

    Used by the registry system to instantiate agents from YAML definitions.
    Falls back to settings for guardrails not specified in config.

    `temperature` overrides settings.default_temperature for this agent
    instance. Set per-request via the OpenAI-compat `temperature` field.
    """
    from app.registry.tool_registry import resolve_tools
    from app.context.request_context import set_agent_contract
    from app.runtime.tool_authorization import wrap_tool_with_authorization

    set_agent_contract(config.id, config.knowledge.get("collection", ""))

    model = _create_chat_model_with_fallback(config.model, temperature=temperature)
    tools = resolve_tools(config.tools)
    max_output = config.guardrails.get("max_tool_output", settings.max_tool_output)
    truncated_tools = [_truncate_tool_output(t, max_output) for t in tools]
    execution_tools = [wrap_tool_with_authorization(t) for t in truncated_tools]

    prompt = config.prompt or get_system_prompt()
    return create_react_agent(
        model=model,
        tools=execution_tools,
        prompt=_make_state_modifier(prompt),
        checkpointer=_get_checkpointer(),
    )


def _select_model(user_input: str) -> str:
    """Select model based on query complexity."""
    from app.agent.intent import classify_complexity

    complexity = classify_complexity(user_input)
    if complexity == "complex" and settings.strong_model != settings.default_model:
        return settings.strong_model
    if complexity == "simple" and settings.cheap_model != settings.default_model:
        return settings.cheap_model
    return settings.default_model


async def get_agent(
    model_name: str | None = None,
    temperature: float | None = None,
    tenant_id: str | None = None,
    session_id: str | None = None,
    prompt_version: str | None = None,
):
    """Compatibility delegate to the Engine-owned Agent registry."""
    from app.agent.engine.registry import get_agent_registry

    return await get_agent_registry().get_agent(
        model_name,
        temperature=temperature,
        tenant_id=tenant_id,
        session_id=session_id,
        prompt_version=prompt_version,
    )
