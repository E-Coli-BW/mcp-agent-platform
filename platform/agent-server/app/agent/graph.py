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

import asyncio
import functools
from typing import TYPE_CHECKING

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import BaseTool
from langgraph.checkpoint.memory import MemorySaver
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
    """Factory: create the right LangChain ChatModel based on provider.

    WHY A FACTORY? LangGraph's create_react_agent() requires the model to 
    implement bind_tools() — this method converts our tool definitions into
    the provider's native format (e.g., Ollama's JSON tools parameter,
    OpenAI's function calling format). Each provider has a DIFFERENT format,
    so we MUST use the native LangChain class for each provider.
    
    WHY NOT A GENERIC WRAPPER? (Pitfall #20)
    We originally tried a custom ChatModel wrapper that would work with any
    provider. It failed because bind_tools() needs provider-specific knowledge:
    - Ollama: sends tools as JSON in the request body
    - OpenAI: sends as 'functions' or 'tools' parameter
    - Anthropic: sends as 'tools' with different schema format
    A generic wrapper can't know these differences.
    
    LAZY IMPORTS: Provider-specific packages (langchain_openai, langchain_ollama)
    are imported inside the if-branches to avoid ImportError when a provider's
    package isn't installed. This means you only need `pip install langchain-ollama`
    if you're using Ollama, not all providers.

    Format: "provider/model" or just "model" (defaults to ollama)
    Examples: "qwen2.5:7b", "openai/gpt-4o", "anthropic/claude-sonnet-4-20250514"

    `temperature`: per-call override. None ⇒ settings.default_temperature.
    Used by the chat API to honour the OpenAI-compatible `temperature`
    field on incoming requests (previously dropped silently before this
    was wired through).
    """
    name = model_name or settings.default_model
    # Resolve temperature once so all branches get the same value; lets the
    # caller pass 0.0 (which is falsy but a legitimate override) without
    # the `or` trick swallowing it.
    temp = temperature if temperature is not None else settings.default_temperature

    if name.startswith("openai/") or name.startswith("gpt-"):
        from langchain_openai import ChatOpenAI

        model_id = name.replace("openai/", "")
        return ChatOpenAI(
            model=model_id,
            api_key=settings.openai_api_key,
            temperature=temp,
            max_tokens=settings.max_tokens,
        )

    elif name.startswith("anthropic/") or name.startswith("claude"):
        # Anthropic via OpenAI-compatible endpoint
        # NOTE: This uses ChatOpenAI class but points to Anthropic's API
        # because Anthropic's official endpoint is OpenAI-compatible for chat
        from langchain_openai import ChatOpenAI

        model_id = name.replace("anthropic/", "")
        return ChatOpenAI(
            model=model_id,
            api_key=settings.anthropic_api_key,
            base_url="https://api.anthropic.com/v1",
            temperature=temp,
            max_tokens=settings.max_tokens,
        )

    elif name.startswith("mlx/"):
        # MLX local server (Apple Silicon native, OpenAI-compatible API)
        # Runs on localhost:8600 via platform/llm-infra/serving/server.py
        # Much faster than Ollama for small models (165 tok/s vs 37 tok/s)
        # NOTE: streaming=False because MLX server doesn't support SSE streaming.
        # Without this, LangGraph's astream_events gets an empty stream →
        # "No generations found in stream" error in reflexion/reranking.
        from langchain_openai import ChatOpenAI

        model_id = name.replace("mlx/", "")
        return ChatOpenAI(
            model=model_id,
            api_key="mlx-local",  # placeholder, MLX server doesn't need auth
            base_url=settings.mlx_base_url + "/v1",
            temperature=temp,
            max_tokens=settings.max_tokens,
            streaming=False,
        )

    else:
        # Default: Ollama (local inference, no API key needed)
        # Ollama runs on localhost:11434 by default
        # ChatOllama handles tool calling via Ollama's native tool API
        from langchain_ollama import ChatOllama

        model_id = name.replace("ollama/", "")
        return ChatOllama(
            model=model_id,
            base_url=settings.ollama_base_url,
            temperature=temp,
            # Note: no max_tokens for Ollama — it uses num_predict in options
        )


def _create_chat_model_with_fallback(
    model_name: str | None = None,
    temperature: float | None = None,
) -> BaseChatModel:
    """Create primary model wrapped with fallback chain (if configured).

    If AGENT_FALLBACK_MODEL is set, wraps primary in ChatModelWithFallback.
    Otherwise returns primary directly (zero overhead).
    """
    primary = _create_chat_model(model_name, temperature=temperature)

    if not settings.fallback_model:
        return primary

    from app.agent.model_fallback import ChatModelWithFallback
    fallback = _create_chat_model(settings.fallback_model, temperature=temperature)
    return ChatModelWithFallback(
        primary=primary,
        fallback=fallback,
        primary_name=model_name or settings.default_model,
        fallback_name=settings.fallback_model,
    )


# ── Agent Factory ─────────────────────────────────────────────

# ── Checkpointer: conversation state persistence ──────────────
# Determines where conversation history is stored.
# - MemorySaver: in-memory (dev only, lost on restart)
# - RedisSaver: persistent, survives restarts, shared across workers

def _create_checkpointer():
    """Create the best available checkpointer.

    Priority:
    1. Redis (if REDIS_URL is set and connection succeeds)
    2. PostgreSQL (if POSTGRES_URL is set)
    3. MemorySaver (fallback, in-memory only)

    GOTCHA: ``RedisSaver.from_conn_string()`` and ``PostgresSaver.from_conn_string()``
    return **context managers**, not savers. Using the return value directly
    gives ``TypeError: Invalid checkpointer ... Received _GeneratorContextManager``
    from ``create_react_agent``. We have to enter the context to obtain the
    real saver. Because we want the saver to live for the lifetime of the
    process, we keep the context manager around as ``_cm_owner`` so it's not
    garbage-collected (which would close the underlying connection pool).
    """
    import os
    import logging
    _log = logging.getLogger(__name__)

    # 1) Redis — but only if REDIS_URL is *explicitly* set. Otherwise we'd
    # try to connect to localhost:6379 on every boot and get noisy timeouts
    # in dev (where Redis is usually not running).
    redis_url = os.environ.get("REDIS_URL")
    if redis_url:
        try:
            from langgraph.checkpoint.redis import RedisSaver

            cm = RedisSaver.from_conn_string(redis_url)
            checkpointer = cm.__enter__()
            # Pin the context manager to the saver so its connection pool
            # isn't closed when this function returns.
            checkpointer._cm_owner = cm  # type: ignore[attr-defined]
            checkpointer.setup()  # create indices if needed
            _log.info("✅ Using RedisSaver for conversation persistence (%s)", redis_url)
            return checkpointer
        except ImportError:
            _log.info("langgraph-checkpoint-redis not installed, trying postgres...")
        except Exception as e:
            _log.warning("Redis unavailable (%s), trying postgres...", e)

    # 2) PostgreSQL
    pg_url = os.environ.get("POSTGRES_URL")
    if pg_url:
        try:
            from langgraph.checkpoint.postgres import PostgresSaver

            cm = PostgresSaver.from_conn_string(pg_url)
            checkpointer = cm.__enter__()
            checkpointer._cm_owner = cm  # type: ignore[attr-defined]
            checkpointer.setup()
            _log.info("✅ Using PostgresSaver for conversation persistence")
            return checkpointer
        except ImportError:
            _log.debug("langgraph-checkpoint-postgres not installed")
        except Exception as e:
            _log.debug("PostgreSQL unavailable: %s", e)

    # 3) Fallback: in-memory (data lost on restart)
    _log.warning("⚠️ Using in-memory MemorySaver — conversations lost on restart. "
                 "Set REDIS_URL or POSTGRES_URL for persistence.")
    return MemorySaver()


# Lazy-initialized checkpointer — avoids blocking server startup
# if Redis/Postgres are slow to respond
_checkpointer = None


def _get_checkpointer():
    global _checkpointer
    if _checkpointer is None:
        _checkpointer = _create_checkpointer()
    return _checkpointer


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
    max_output = settings.max_tool_output
    truncated_tools = [_truncate_tool_output(t, max_output) for t in get_tools()]

    agent = create_react_agent(
        model=model,
        tools=truncated_tools,
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

    model = _create_chat_model_with_fallback(config.model, temperature=temperature)
    tools = resolve_tools(config.tools)
    max_output = config.guardrails.get("max_tool_output", settings.max_tool_output)
    truncated_tools = [_truncate_tool_output(t, max_output) for t in tools]

    prompt = config.prompt or get_system_prompt()
    return create_react_agent(
        model=model,
        tools=truncated_tools,
        prompt=_make_state_modifier(prompt),
        checkpointer=_get_checkpointer(),
    )


# Cached agents keyed by (model_or_config_id, temperature, tenant_id, prompt_hash).
# We include tenant + prompt hash so prompt-governed rollouts don't cross-contaminate
# agent instances across tenants or prompt versions.
_agents: dict[tuple[str, float, str, str], object] = {}
_agent_lock = asyncio.Lock()
_agent_tool_names: dict[tuple[str, float, str, str], set[str]] = {}


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
    """Get or create a cached agent for the requested (model, temperature).

    Prefers YAML-backed agent configs when a matching config id exists.
    Falls back to the legacy dynamic tool-based agent for model names.

    The cache key is (model_name_or_config_id, resolved_temperature) so
    a single process can serve multiple sampling temperatures concurrently
    (e.g. T=0.7 for production chat, T=0 for the eval harness) without
    instance interference. ChatOllama/ChatOpenAI bake `temperature` into
    the model instance, so we MUST get a separate instance per value.
    """
    import logging

    from app.registry.config_loader import load_all_configs
    from app.tools.definitions import get_tools

    # Cache key — resolve temperature here once so identical None / default
    # combinations don't create duplicate cache entries.
    resolved_temp = temperature if temperature is not None else settings.default_temperature
    from app.agent.prompts import resolve_system_prompt
    from app.auth.middleware import tenant_context
    from app.context.request_context import set_prompt_version

    effective_tenant = tenant_id or tenant_context.get() or "default"
    prompt_resolution = resolve_system_prompt(
        requested_version=prompt_version,
        tenant_id=effective_tenant,
        session_id=session_id,
    )
    # Ensure create_agent()/create_agent_v2() sees the same prompt version in this task context.
    set_prompt_version(prompt_resolution.version)

    cache_key: tuple[str, float, str, str] = (
        model_name or "coding-agent",
        resolved_temp,
        effective_tenant,
        prompt_resolution.content_hash,
    )

    async with _agent_lock:
        configs = load_all_configs(settings.agent_config_dir)
        config = configs.get(cache_key[0])

        use_v2 = settings.agent_graph_version == "v2"

        if config is not None:
            current_tool_names = set(config.tools)
            cached_tools = _agent_tool_names.get(cache_key, set())
            if cache_key not in _agents or current_tool_names != cached_tools:
                if cache_key in _agents:
                    logging.getLogger(__name__).info(
                        "🔄 Recreating configured agent %s (T=%.2f): tools changed %s → %s",
                        cache_key[0],
                        cache_key[1],
                        cached_tools,
                        current_tool_names,
                    )
                if use_v2:
                    from app.agent.graph_v2 import create_agent_v2_from_config
                    _agents[cache_key] = create_agent_v2_from_config(
                        config, temperature=temperature
                    )
                else:
                    _agents[cache_key] = create_agent_from_config(
                        config, temperature=temperature
                    )
                _agent_tool_names[cache_key] = current_tool_names
            return _agents[cache_key]

        current_tools = get_tools()
        current_tool_names = set(t.name for t in current_tools)
        cached_tools = _agent_tool_names.get(cache_key, set())
        resolved_model = settings.default_model if cache_key[0] == "coding-agent" else cache_key[0]
        if cache_key not in _agents or current_tool_names != cached_tools:
            if cache_key in _agents:
                logging.getLogger(__name__).info(
                    "🔄 Recreating fallback agent %s (T=%.2f): tool set changed %s → %s",
                    cache_key[0],
                    cache_key[1],
                    cached_tools,
                    current_tool_names,
                )
            if use_v2:
                from app.agent.graph_v2 import create_agent_v2
                _agents[cache_key] = create_agent_v2(
                    resolved_model, temperature=temperature
                )
            else:
                _agents[cache_key] = create_agent(
                    resolved_model, temperature=temperature
                )
            _agent_tool_names[cache_key] = current_tool_names
        return _agents[cache_key]
