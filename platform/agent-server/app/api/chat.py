"""OpenAI-compatible chat completions API with SSE streaming."""

import asyncio
import json
import logging
import os
import re
import time
import uuid
from typing import AsyncIterator

from fastapi import APIRouter, HTTPException, Depends, Request
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse
from langchain_core.messages import HumanMessage

from app.agent.graph import get_agent
from app.agent.prompts import resolve_system_prompt
from app.agent.fleet_bus import (
    register_session as fleet_register,
    unregister_session as fleet_unregister,
    subscribe as fleet_subscribe,
    request_cancel as fleet_request_cancel,
)
from app.store.conversation import get_conversation_store, Message
from app.agent.intent import is_meta_question, get_meta_answer, detect_topic_switch
from app.usage import get_usage_tracker, RequestUsage, estimate_tokens
from app.auth.middleware import require_auth, AuthContext, tenant_context
from app.config import settings
from app.context.request_context import (
    get_request_context,
    set_prompt_version,
    set_request_context,
)
from app.events.model_provenance import (
    build_feature_flags_snapshot,
    current_trace_id,
    infer_provider,
    make_model_call_event,
)
from app.runtime import RunStatus, get_run_recorder, get_idempotency_store
from app.runtime.cancellation import get_run_cancellation_registry
from app.runtime.policy import get_policy_evaluator
from app.runtime.tool_invocation import make_tool_invocation_envelope
from app.runtime.approval import get_approval_registry
from app.runtime.checkpoint import get_checkpoint_store
from app.runtime.tool_authorization import (
    reset_approval_notifier,
    set_approval_notifier,
)

router = APIRouter()
logger = logging.getLogger("agent.chat")


# ── Request/Response Models (OpenAI-compatible) ──────────────

class ChatMessage(BaseModel):
    role: str
    content: str


class ActiveFileContext(BaseModel):
    """Context about the file currently open in the user's editor."""

    path: str
    visible_start: int | None = None
    visible_end: int | None = None


class ChatRequest(BaseModel):
    model: str = "coding-agent"
    messages: list[ChatMessage]
    stream: bool = True
    temperature: float | None = None
    max_tokens: int | None = None
    prompt_version: str | None = None  # optional governed prompt override (if enabled)
    session_id: str | None = None  # for conversation persistence
    active_file: ActiveFileContext | None = None


# ── SSE Streaming ─────────────────────────────────────────────

def _make_chunk(content: str, model: str, finish_reason: str | None = None) -> str:
    """Format a single SSE chunk in OpenAI format."""
    chunk = {
        "id": f"chatcmpl-{uuid.uuid4().hex[:8]}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "delta": {"content": content} if content else {},
            "finish_reason": finish_reason,
        }],
    }
    return json.dumps(chunk)


def _make_tool_event(
    action: str,
    tool_name: str,
    data: dict,
    seq: int,
    event_id: str | None = None,
) -> dict:
    """Create a named SSE tool event (separate from content stream)."""
    event = {
        "event": f"tool_{action}",
        "data": json.dumps({
            "tool": tool_name,
            "seq": seq,
            **data,
        }),
    }
    if event_id:
        event["id"] = event_id
    return event


def _make_status_event(state: str, event_id: str | None = None, **extra) -> dict:
    """Create a named SSE status event."""
    event = {
        "event": "status",
        "data": json.dumps({"state": state, **extra}),
    }
    if event_id:
        event["id"] = event_id
    return event


def _make_child_event(bus_event: dict) -> dict:
    """Convert a fleet_bus event into a named SSE event for the browser.

    The bus event's ``type`` becomes the SSE ``event:`` name (e.g.
    ``child_token``); the rest of the dict becomes the JSON payload.
    Frontend dashboards can listen by SSE event name and ignore unknown
    types gracefully — same pattern as ``tool_start`` / ``status``.
    """
    return {
        "event": bus_event["type"],
        "data": json.dumps({
            "child_session_id": bus_event.get("child_session_id"),
            "role": bus_event.get("role"),
            **{k: v for k, v in bus_event.items()
               if k not in ("type", "root_session_id", "child_session_id", "role")},
        }),
    }


async def _stream_agent_response(
    messages: list[ChatMessage],
    model: str,
    session_id: str,
    tenant_id: str = "default",
    temperature: float | None = None,
    max_tokens: int | None = None,
    prompt_version: str | None = None,
    http_request: Request | None = None,
) -> AsyncIterator[str | dict]:
    """Stream agent response as OpenAI-compatible SSE events.

    `temperature`: forwarded to `get_agent`. None ⇒ server-default
    (`settings.default_temperature`). Was silently dropped before this
    change — see latent bug D in EVAL-ITERATION-REPORT-2026-05-29.md.
    The agent cache is keyed on (model, temperature) so concurrent T=0
    and T=0.7 requests get distinct model instances.
    """
    prompt_resolution = resolve_system_prompt(
        requested_version=prompt_version,
        tenant_id=tenant_id,
        session_id=session_id,
    )
    set_prompt_version(prompt_resolution.version)

    agent = await get_agent(
        model,
        temperature=temperature,
        tenant_id=tenant_id,
        session_id=session_id,
        prompt_version=prompt_resolution.version,
    )
    store = get_conversation_store()
    req_ctx = get_request_context()
    flag_snapshot = build_feature_flags_snapshot()
    runtime_name = f"python-{settings.agent_graph_version}"
    effective_temperature = temperature if temperature is not None else settings.default_temperature
    run_recorder = get_run_recorder()

    # Register the fleet bus for this session so any spawn_subagent calls
    # inside the agent loop can publish child events. We subscribe to it
    # in parallel and merge those events into our SSE output. Tear-down
    # happens in the `finally` block — leaking a registration would let
    # late-arriving child events queue up against a dead session forever.
    await fleet_register(session_id)
    bus_subscription = None

    # Save user message to conversation store
    user_input = messages[-1].content if messages else ""
    await store.append(session_id, Message(role="user", content=user_input))

    input_preview = (user_input[:120] + "...") if len(user_input) > 120 else user_input
    logger.info(
        "📩 收到用户消息 [session=%s, model=%s]: %s",
        session_id, model, input_preview,
    )

    config = {
        "configurable": {"thread_id": session_id},
        "recursion_limit": settings.max_agent_steps * 4,  # LangGraph internal limit; v2 uses its own max_steps guard
    }
    full_response = ""
    lock_acquired = False
    client_disconnected = False
    cancel_requested = False

    try:
        # Token tracking
        # ── prompt_tokens NOTE ──────────────────────────────────────────
        # We DON'T initialize prompt_tokens from user_input alone — that was
        # the historical bug that made every request log `prompt=8` and made
        # cost calculation always zero. The real prompt sent to the LLM
        # includes: system prompt + workspace context + full conversation
        # history + tool definitions, which is 10x-100x larger than the
        # user's last turn.
        #
        # Instead we accumulate prompt tokens from `on_chat_model_start`
        # events below — each event carries the actual input messages the
        # LLM is about to see. For multi-turn ReAct loops this fires once
        # per tool round (LLM → tool → LLM → tool → ...), so we get the
        # sum across all LLM calls in this request, which is what we want
        # for cost tracking.
        # ────────────────────────────────────────────────────────────────
        usage = RequestUsage(model=model)
        tool_count = 0
        start_time = time.time()
        model_call_seq = 0
        active_model_call: dict | None = None

        # Reranker feedback tracking
        last_rag_query = None
        last_rag_chunks = []
        retrieval_trace_ids: list[str] = []
        file_reads_after_rag = []
        tools_since_rag = 0  # count tool calls since last rag_search

        await run_recorder.start_run(
            run_id=req_ctx.run_id,
            request_id=req_ctx.request_id,
            tenant_id=tenant_id,
            session_id=session_id,
            model=model,
            metadata={
                "runtime": runtime_name,
                "prompt_version": prompt_resolution.version,
                "prompt_hash": prompt_resolution.content_hash,
                "temperature": effective_temperature,
            },
        )
        cancellation = get_run_cancellation_registry()
        await cancellation.register(req_ctx.run_id)
        status_event = await run_recorder.record_event(
            req_ctx.run_id,
            "status_thinking",
            {"request_id": req_ctx.request_id},
        )
        yield _make_status_event(
            "thinking",
            event_id=status_event.event_id,
            request_id=req_ctx.request_id,
            run_id=req_ctx.run_id,
        )

        # Test-only pause makes cancellation acceptance deterministic. It is
        # opt-in and runs after the run id is visible to the client.
        test_delay_ms = int(os.environ.get("AGENT_TEST_DELAY_MS", "0"))
        if test_delay_ms > 0:
            await asyncio.sleep(test_delay_ms / 1000)

        # Session LANE — prevent interleaved requests for same session
        from app.middleware.session_lane import (
            release_session_lock,
            session_lock_held,
            wait_for_session_lock,
        )

        was_queued = await session_lock_held(session_id)
        if was_queued:
            yield _make_status_event(
                "queued",
                request_id=req_ctx.request_id,
                run_id=req_ctx.run_id,
                message="Your message is queued behind the active turn",
            )

        if not await wait_for_session_lock(session_id, max_wait=900):
            yield _make_chunk(
                "⚠️ Another request for this session is still processing. Please wait.",
                model,
                finish_reason="stop",
            )
            yield "[DONE]"
            return
        lock_acquired = True

        # Stream agent events.
        #
        # We MERGE two sources here:
        #   (1) the parent agent's astream_events (its own LLM tokens + tool calls)
        #   (2) the fleet bus — child agents publishing tokens/tool calls while
        #       the parent is blocked inside a `spawn_subagent` tool call
        #
        # Each event is tagged with its source so the dispatch switch below
        # can route it to the right SSE shape. We use an asyncio.Queue as
        # the fan-in point — both producer tasks push, the consumer (this
        # generator) pops. The queue is unbounded because both producers
        # are already rate-limited by the upstream sources (LLM streaming
        # rate / fleet bus per-subscriber queue cap).
        merge_q: asyncio.Queue = asyncio.Queue()
        # Sentinel object distinguishable from any real event.
        _AGENT_DONE = object()

        async def _notify_approval(approval):
            await merge_q.put(("approval_required", approval))

        approval_notifier_token = set_approval_notifier(_notify_approval)

        async def _drain_agent():
            try:
                async for ev in agent.astream_events(
                    {"messages": [HumanMessage(content=user_input)]},
                    config=config,
                    version="v2",
                ):
                    await merge_q.put(("agent", ev))
            except Exception as exc:  # surface agent errors via the queue
                await merge_q.put(("agent_error", exc))
            finally:
                await merge_q.put(("agent_done", _AGENT_DONE))

        async def _drain_bus():
            # Bus subscription terminates when fleet_unregister fires
            # (which we do in the outer `finally`). Until then this task
            # forwards every event into the merge queue.
            nonlocal bus_subscription
            bus_subscription = fleet_subscribe(session_id)
            try:
                async for ev in bus_subscription:
                    await merge_q.put(("bus", ev))
            except asyncio.CancelledError:
                pass

        agent_task = asyncio.create_task(_drain_agent())
        bus_task = asyncio.create_task(_drain_bus())

        try:
            _HEARTBEAT_INTERVAL = 15  # seconds
            while True:
                # Check if client disconnected (SSE long-poll protection)
                if http_request is not None and await http_request.is_disconnected():
                    logger.info("🔌 Client disconnected [session=%s], aborting stream", session_id)
                    client_disconnected = True
                    await run_recorder.record_event(
                        req_ctx.run_id,
                        "client_disconnected",
                        {"session_id": session_id},
                    )
                    agent_task.cancel()
                    break

                if await cancellation.is_requested(req_ctx.run_id):
                    logger.info("⏹ Root run cancellation requested [run=%s]", req_ctx.run_id)
                    cancel_requested = True
                    await run_recorder.record_event(
                        req_ctx.run_id,
                        "cancel_requested",
                        {"request_id": req_ctx.request_id, "session_id": session_id},
                    )
                    agent_task.cancel()
                    break

                try:
                    source, event = await asyncio.wait_for(
                        merge_q.get(), timeout=_HEARTBEAT_INTERVAL
                    )
                except asyncio.TimeoutError:
                    # No event within interval — send SSE keepalive comment
                    yield ": keepalive\n"
                    continue

                if source == "agent_done":
                    break
                if source == "agent_error":
                    raise event  # re-raise into the outer try/except below

                if source == "approval_required":
                    yield {
                        "event": "approval_required",
                        "data": json.dumps(event.to_dict()),
                    }
                    continue

                if source == "bus":
                    # Forward child fleet events to the browser unchanged
                    # (just re-wrapped as a named SSE event).
                    yield _make_child_event(event)
                    continue

                # source == "agent"
                kind = event.get("event", "")

                # Accumulate REAL prompt tokens — fires once per LLM call inside
                # the ReAct loop. The `input` here is the full message list the
                # LLM is about to see (system + history + tool ToolMessages +
                # current user turn). estimate_tokens() is rough (4 chars≈1tok)
                # but at least it's measuring the right thing.
                if kind == "on_chat_model_start":
                    model_input = event.get("data", {}).get("input", {})
                    messages_for_llm = model_input.get("messages", []) if isinstance(model_input, dict) else []
                    # messages_for_llm is a list of message-lists per langgraph's API;
                    # flatten one level if needed.
                    if messages_for_llm and isinstance(messages_for_llm[0], list):
                        messages_for_llm = messages_for_llm[0]
                    call_prompt_tokens = 0
                    for msg in messages_for_llm:
                        content = getattr(msg, "content", None)
                        if isinstance(content, str):
                            t = estimate_tokens(content)
                            usage.prompt_tokens += t
                            call_prompt_tokens += t
                        elif isinstance(content, list):
                            # Multimodal / structured content — sum text parts only.
                            for part in content:
                                if isinstance(part, dict) and isinstance(part.get("text"), str):
                                    t = estimate_tokens(part["text"])
                                    usage.prompt_tokens += t
                                    call_prompt_tokens += t

                    model_call_seq += 1
                    active_model_call = {
                        "seq": model_call_seq,
                        "started_at": time.monotonic(),
                        "prompt_tokens": call_prompt_tokens,
                        "completion_tokens": 0,
                    }
                    await run_recorder.record_event(
                        req_ctx.run_id,
                        "model_call_started",
                        {
                            "seq": model_call_seq,
                            "provider": infer_provider(model),
                            "model": model,
                            "prompt_tokens": call_prompt_tokens,
                            "prompt_version": prompt_resolution.version,
                            "prompt_hash": prompt_resolution.content_hash,
                        },
                    )

                # Stream LLM tokens
                if kind == "on_chat_model_stream":
                    chunk = event.get("data", {}).get("chunk")
                    if chunk and hasattr(chunk, "content") and chunk.content:
                        full_response += chunk.content
                        inc = estimate_tokens(chunk.content)
                        usage.completion_tokens += inc
                        if active_model_call is not None:
                            active_model_call["completion_tokens"] += inc
                        yield _make_chunk(chunk.content, model)

                elif kind == "on_chat_model_end":
                    if active_model_call is not None:
                        duration_ms = int((time.monotonic() - active_model_call["started_at"]) * 1000)
                        model_event = make_model_call_event(
                            run_id=req_ctx.run_id,
                            request_id=req_ctx.request_id,
                            trace_id=current_trace_id(),
                            tenant_id=tenant_id,
                            session_id=session_id,
                            runtime=runtime_name,
                            call_site=f"chat._stream_agent_response.llm_call_{active_model_call['seq']}",
                            provider=infer_provider(model),
                            model=model,
                            temperature=effective_temperature,
                            max_tokens=max_tokens,
                            prompt_id=prompt_resolution.prompt_id,
                            prompt_version=prompt_resolution.version,
                            prompt_hash=prompt_resolution.content_hash,
                            feature_flags=flag_snapshot,
                            prompt_tokens=active_model_call["prompt_tokens"],
                            completion_tokens=active_model_call["completion_tokens"],
                            duration_ms=duration_ms,
                            status="ok",
                        )
                        from app.events.kafka_producer import get_event_producer
                        await get_event_producer().emit_model_call(model_event)
                        await run_recorder.record_event(
                            req_ctx.run_id,
                            "model_call_completed",
                            {
                                "seq": active_model_call["seq"],
                                "provider": infer_provider(model),
                                "model": model,
                                "prompt_tokens": active_model_call["prompt_tokens"],
                                "completion_tokens": active_model_call["completion_tokens"],
                                "duration_ms": duration_ms,
                                "status": "ok",
                            },
                        )
                        active_model_call = None

                # Log tool calls — emit structured event for UI
                elif kind == "on_tool_start":
                    tool_name = event.get("name", "unknown")
                    tool_input = event.get("data", {}).get("input", {})
                    tool_count += 1
                    tool_envelope = make_tool_invocation_envelope(
                        run_id=req_ctx.run_id,
                        request_id=req_ctx.request_id,
                        session_id=session_id,
                        tenant_id=tenant_id,
                        step_seq=tool_count,
                        tool_name=tool_name,
                        arguments=tool_input if isinstance(tool_input, dict) else {"input": tool_input},
                    )
                    policy_decision = get_policy_evaluator().evaluate(tool_envelope)

                    # Publish to Kafka (fire-and-forget, no blocking)
                    from app.events.kafka_producer import get_event_producer
                    await get_event_producer().emit_tool_start(
                        session_id, tool_name, tool_input, model
                    )

                    # Track rag_search → file_read sequences for feedback
                    # Only count file_reads within 3 tool calls of rag_search (high confidence)
                    if tool_name == "rag_search":
                        last_rag_query = tool_input.get("query", "")
                        last_rag_chunks = []
                        file_reads_after_rag = []
                        tools_since_rag = 0
                    elif tool_name == "file_read" and last_rag_query and tools_since_rag <= 3:
                        file_reads_after_rag.append(tool_input.get("path", ""))
                    if last_rag_query:
                        tools_since_rag += 1

                    tool_started_event = await run_recorder.record_event(
                        req_ctx.run_id,
                        "tool_started",
                        {
                            "seq": tool_count,
                            "tool": tool_name,
                            "input": tool_input,
                            "invocation": tool_envelope.to_event_payload(),
                            "policy_decision": policy_decision.to_event_payload(),
                        },
                    )
                    yield _make_tool_event(
                        "start",
                        tool_name,
                        {"input": tool_input},
                        seq=tool_count,
                        event_id=tool_started_event.event_id,
                    )

                    # OLD: HTML comment for backward compat (remove after frontend migration)
                    tool_event = json.dumps(
                        {"tool": tool_name, "action": "start", "input": tool_input}
                    )
                    yield _make_chunk(
                        f'\n<!-- TOOL:{tool_event} -->\n🔧 Using {tool_name}...\n',
                        model,
                    )

                elif kind == "on_tool_end":
                    tool_name = event.get("name", "unknown")
                    tool_output = str(event.get("data", {}).get("output", ""))[:500]
                    tool_input_end = event.get("data", {}).get("input", {})

                    # Reconstruct the candidate set for the implicit
                    # reranker label. Tool events expose the rendered output,
                    # not the ScoredChunk objects, so query the same tenant
                    # index once after rag_search completes. This keeps the
                    # feedback path independent from display formatting.
                    if tool_name == "rag_search" and last_rag_query:
                        try:
                            from app.rag.index.retriever import get_retriever_for_tenant
                            retriever = await get_retriever_for_tenant(
                                tenant_id,
                                collection=get_request_context().knowledge_collection or None,
                            )
                            last_rag_chunks = await retriever.search(last_rag_query, top_k=5)
                            from app.feedback.store import record_retrieval_trace
                            trace = record_retrieval_trace(
                                run_id=req_ctx.run_id,
                                session_id=session_id,
                                tenant_id=tenant_id,
                                agent=model,
                                query=last_rag_query,
                                chunks=[{
                                    "path": chunk.file_path,
                                    "name": chunk.name,
                                    "score": chunk.score,
                                    "start_line": chunk.start_line,
                                    "end_line": chunk.end_line,
                                } for chunk in last_rag_chunks],
                                embedding_version=os.environ.get("AGENT_EMBEDDING_MODEL", "mxbai-embed-large"),
                                reranker_version=os.environ.get("AGENT_RERANKER", "learned"),
                                collection=get_request_context().knowledge_collection or "default",
                            )
                            retrieval_trace_ids.append(trace["trace_id"])
                        except Exception:
                            last_rag_chunks = []

                    tool_completed_event = await run_recorder.record_event(
                        req_ctx.run_id,
                        "tool_completed",
                        {
                            "seq": tool_count,
                            "tool": tool_name,
                            "input": tool_input_end,
                            "output_preview": tool_output[:200],
                        },
                    )
                    yield _make_tool_event(
                        "end",
                        tool_name,
                        {"input": tool_input_end, "output": tool_output[:200]},
                        seq=tool_count,
                        event_id=tool_completed_event.event_id,
                    )

                    # OLD: HTML comment for backward compat
                    tool_event = json.dumps({
                        "tool": tool_name,
                        "action": "end",
                        "input": tool_input_end,
                        "output": tool_output,
                    })
                    yield _make_chunk(f'<!-- TOOL:{tool_event} -->\n', model)

                    # Publish to Kafka
                    from app.events.kafka_producer import get_event_producer
                    await get_event_producer().emit_tool_end(session_id, tool_name, tool_output)
        finally:
            # Stop the bus drainer first so it stops queuing new events,
            # then the agent task (which should already be done by the
            # time we reach here on the happy path). Both are no-ops if
            # they've already finished.
            bus_task.cancel()
            agent_task.cancel()
            # Suppress exceptions raised by cancellation — we already
            # surfaced any real error via the merge queue.
            for t in (bus_task, agent_task):
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass
            reset_approval_notifier(approval_notifier_token)

        if active_model_call is not None:
            duration_ms = int((time.monotonic() - active_model_call["started_at"]) * 1000)
            model_event = make_model_call_event(
                run_id=req_ctx.run_id,
                request_id=req_ctx.request_id,
                trace_id=current_trace_id(),
                tenant_id=tenant_id,
                session_id=session_id,
                runtime=runtime_name,
                call_site=f"chat._stream_agent_response.llm_call_{active_model_call['seq']}",
                provider=infer_provider(model),
                model=model,
                temperature=effective_temperature,
                max_tokens=max_tokens,
                prompt_id=prompt_resolution.prompt_id,
                prompt_version=prompt_resolution.version,
                prompt_hash=prompt_resolution.content_hash,
                feature_flags=flag_snapshot,
                prompt_tokens=active_model_call["prompt_tokens"],
                completion_tokens=active_model_call["completion_tokens"],
                duration_ms=duration_ms,
                status="ok",
            )
            from app.events.kafka_producer import get_event_producer
            await get_event_producer().emit_model_call(model_event)
            await run_recorder.record_event(
                req_ctx.run_id,
                "model_call_completed",
                {
                    "seq": active_model_call["seq"],
                    "provider": infer_provider(model),
                    "model": model,
                    "prompt_tokens": active_model_call["prompt_tokens"],
                    "completion_tokens": active_model_call["completion_tokens"],
                    "duration_ms": duration_ms,
                    "status": "ok",
                },
            )

        # Send finish + save response
        if full_response:
            await store.append(session_id, Message(role="assistant", content=full_response))

        # Record usage
        usage.tool_calls = tool_count
        usage.duration_ms = int((time.time() - start_time) * 1000)
        get_usage_tracker().record(usage)

        response_preview = (full_response[:120] + "...") if len(full_response) > 120 else full_response
        logger.info(
            "📤 Agent 响应 [session=%s, tools=%d, %dms]: %s",
            session_id, tool_count, usage.duration_ms, response_preview.replace("\n", " "),
        )

        # Publish response summary to Kafka
        from app.events.kafka_producer import get_event_producer
        await get_event_producer().emit_agent_response(
            session_id, model,
            usage.prompt_tokens + usage.completion_tokens,
            usage.duration_ms
        )

        # Log reranker feedback (which chunks the agent actually used)
        if last_rag_query and file_reads_after_rag:
            try:
                from app.rag.reranking.learned import get_learned_reranker
                reranker = get_learned_reranker()
                reranker.log_feedback(last_rag_query, last_rag_chunks, file_reads_after_rag)
                # Auto-retrain every 50 feedback entries
                info = reranker.get_weights_info()
                if info["feedback_count"] > 0 and info["feedback_count"] % 50 == 0:
                    reranker.retrain()
            except Exception:
                pass  # non-critical

        run_finished_event = await run_recorder.finish_run(
            req_ctx.run_id,
            RunStatus.CANCELLED if client_disconnected or cancel_requested else RunStatus.COMPLETED,
            {
                "request_id": req_ctx.request_id,
                "session_id": session_id,
                "tool_count": tool_count,
                "duration_ms": usage.duration_ms,
                "prompt_tokens": usage.prompt_tokens,
                "completion_tokens": usage.completion_tokens,
                "terminal_reason": (
                    "client_disconnected" if client_disconnected
                    else "cancel_requested" if cancel_requested else "completed"
                ),
                "retryable": True if client_disconnected or cancel_requested else False,
            },
        )
        yield _make_status_event(
            "cancelled" if client_disconnected or cancel_requested else "complete",
            event_id=run_finished_event.event_id,
            request_id=req_ctx.request_id,
            run_id=req_ctx.run_id,
            tool_count=tool_count,
            duration_ms=int((time.time() - start_time) * 1000),
            retrieval_trace_ids=retrieval_trace_ids,
        )
        yield _make_chunk('', model, finish_reason='stop')
        yield "[DONE]"

    except Exception as e:
        if active_model_call is not None:
            duration_ms = int((time.monotonic() - active_model_call["started_at"]) * 1000)
            model_event = make_model_call_event(
                run_id=req_ctx.run_id,
                request_id=req_ctx.request_id,
                trace_id=current_trace_id(),
                tenant_id=tenant_id,
                session_id=session_id,
                runtime=runtime_name,
                call_site=f"chat._stream_agent_response.llm_call_{active_model_call['seq']}",
                provider=infer_provider(model),
                model=model,
                temperature=effective_temperature,
                max_tokens=max_tokens,
                prompt_id=prompt_resolution.prompt_id,
                prompt_version=prompt_resolution.version,
                prompt_hash=prompt_resolution.content_hash,
                feature_flags=flag_snapshot,
                prompt_tokens=active_model_call["prompt_tokens"],
                completion_tokens=active_model_call["completion_tokens"],
                duration_ms=duration_ms,
                status="error",
                error_class=e.__class__.__name__,
            )
            from app.events.kafka_producer import get_event_producer
            await get_event_producer().emit_model_call(model_event)
            await run_recorder.record_event(
                req_ctx.run_id,
                "model_call_completed",
                {
                    "seq": active_model_call["seq"],
                    "provider": infer_provider(model),
                    "model": model,
                    "prompt_tokens": active_model_call["prompt_tokens"],
                    "completion_tokens": active_model_call["completion_tokens"],
                    "duration_ms": duration_ms,
                    "status": "error",
                    "error_class": e.__class__.__name__,
                },
            )

        retryable = not isinstance(e, (ValueError, PermissionError, KeyError))
        await run_recorder.finish_run(
            req_ctx.run_id,
            RunStatus.FAILED,
            {
                "request_id": req_ctx.request_id,
                "session_id": session_id,
                "error_class": e.__class__.__name__,
                "error_message": str(e),
                "terminal_reason": "agent_error",
                "retryable": retryable,
            },
        )
        error_msg = f"\n\n❌ Agent error: {str(e)}"
        yield _make_chunk(error_msg, model, finish_reason="stop")
        yield "[DONE]"
    finally:
        if lock_acquired:
            await release_session_lock(session_id)
        # Tear down the fleet bus for this session. Any child agents
        # still running after this will publish into a dead bus and
        # silently drop their events — that's by design (see fleet_bus
        # module docstring). The alternative (waiting for children) would
        # let a stuck spawn block the parent's HTTP response forever.
        await fleet_unregister(session_id)
        await get_run_cancellation_registry().unregister(req_ctx.run_id)


# ── Endpoints ─────────────────────────────────────────────────

@router.post("/v1/chat/completions")
async def chat_completions(
    raw_request: Request,
    request: ChatRequest,
    auth: AuthContext = Depends(require_auth),
):
    """OpenAI-compatible chat completions endpoint with streaming.

    Pre-processing:
    1. Meta-questions → answer directly (no agent/tools)
    2. Topic switch → reset agent thread for fresh context
    3. Normal → forward to LangGraph agent
    """
    if not request.messages:
        raise HTTPException(status_code=400, detail="messages is required")

    session_id = request.session_id or f"session-{uuid.uuid4().hex[:12]}"
    session_id = f"{auth.tenant_id}:{session_id}"
    tenant_context.set(auth.tenant_id)
    request_id = f"req-{uuid.uuid4().hex[:12]}"
    run_id = f"run-{uuid.uuid4().hex[:12]}"
    set_request_context(request_id=request_id, run_id=run_id, session_id=session_id)
    set_prompt_version(request.prompt_version or "")

    # Initialize the per-request fleet envelope. Sets the SubagentContext
    # ContextVar so any in-loop spawn_subagent call sees the depth/budget/
    # tool-allowlist policy for THIS request only.
    #
    # The allowed_tools list is the SET of tools the agent may DELEGATE to
    # subagents — by default we let it delegate any tool the root agent
    # already has the right to call. The actual root agent's own tool set
    # is unchanged (it still uses everything in its config).
    #
    # Why initialize even for requests that won't spawn? Cheap (one
    # dataclass), and the alternative — a None context that callers must
    # null-check — has lost us hours of debugging in similar systems.
    from app.agent.subagent_context import init_root_context
    from app.registry.tool_registry import get_registered_tools
    init_root_context(
        root_session_id=session_id,
        # Allow delegation of every currently-registered tool except the
        # spawn tool itself (children must NOT re-spawn — that's the
        # depth-counter's job, but allowlist enforcement is the second
        # line of defense).
        allowed_tools=[n for n in get_registered_tools().keys()
                       if n != "spawn_subagent"],
    )

    user_input = request.messages[-1].content if request.messages else ""

    # Inject active file context into user message
    if request.active_file:
        af = request.active_file
        file_hint = f"[Active file: {af.path}"
        if af.visible_start is not None:
            file_hint += f", lines {af.visible_start}-{af.visible_end}"
        file_hint += "]\n"
        user_input = file_hint + user_input
        request.messages[-1] = ChatMessage(
            role=request.messages[-1].role,
            content=user_input,
        )

    store = get_conversation_store()

    # 1. Meta-questions — answer directly without tools
    if is_meta_question(user_input):
        answer = get_meta_answer(user_input)
        if answer:
            await store.append(session_id, Message(role="user", content=user_input))
            await store.append(session_id, Message(role="assistant", content=answer))

            if request.stream:
                async def meta_stream():
                    yield _make_chunk(answer, request.model)
                    yield _make_chunk('', request.model, finish_reason='stop')
                    yield "[DONE]"
                return EventSourceResponse(meta_stream(), media_type="text/event-stream")
            else:
                return {
                    "id": f"chatcmpl-{uuid.uuid4().hex[:8]}",
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": request.model,
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": answer}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                }

    # 2. Topic switch — use new thread_id so agent starts fresh
    history = await store.get_messages_for_llm(session_id)
    if detect_topic_switch(user_input, history):
        # Append a suffix to thread_id so LangGraph uses fresh state
        session_id = f"{session_id}-{uuid.uuid4().hex[:6]}"
        set_request_context(request_id=request_id, run_id=run_id, session_id=session_id)

    if request.stream:
        return EventSourceResponse(
            _stream_agent_response(
                request.messages, request.model, session_id, auth.tenant_id,
                temperature=request.temperature,
                max_tokens=request.max_tokens,
                prompt_version=request.prompt_version,
                http_request=raw_request,
            ),
            media_type="text/event-stream",
        )
    else:
        # Non-streaming: collect all chunks
        full_response = ""
        async for chunk_str in _stream_agent_response(
            request.messages, request.model, session_id, auth.tenant_id,
            temperature=request.temperature,
            max_tokens=request.max_tokens,
            prompt_version=request.prompt_version,
            http_request=raw_request,
        ):
            if chunk_str == "[DONE]":
                break
            # Skip named SSE events (dicts) — only process content chunks (strings)
            if isinstance(chunk_str, dict):
                continue
            try:
                data = json.loads(chunk_str)
                content = data.get("choices", [{}])[0].get("delta", {}).get("content", "")
                content = re.sub(r"<!-- TOOL:.*? -->\s*", "", content, flags=re.S)
                content = re.sub(r"🔧 Using \w+\.\.\.\s*", "", content)
                full_response += content
            except (json.JSONDecodeError, TypeError):
                pass

        return {
            "id": f"chatcmpl-{uuid.uuid4().hex[:8]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": request.model,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": full_response},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }


@router.get("/v1/models")
async def list_models():
    """List available models."""
    return {
        "object": "list",
        "data": [
            {"id": "coding-agent", "object": "model", "owned_by": "local"},
        ],
    }


@router.get("/v1/runs/{run_id}/events")
async def get_run_events(
    run_id: str,
    after_seq: int = 0,
    auth: AuthContext = Depends(require_auth),
):
    """Return recorded runtime events for a run.

    Phase 2 exposes the in-memory event contract for tests and local
    inspection. Later implementations can keep this endpoint while replacing
    the recorder with a durable store.
    """
    recorder = get_run_recorder()
    metadata = await recorder.get_metadata(run_id)
    if metadata is None:
        raise HTTPException(status_code=404, detail="run not found")
    if metadata["tenant_id"] != auth.tenant_id:
        raise HTTPException(status_code=403, detail="run does not belong to this tenant")

    events = await recorder.list_events(run_id, after_seq=after_seq)
    return {
        "run_id": run_id,
        "status": metadata["status"],
        "terminal_reason": metadata.get("terminal_reason"),
        "retryable": metadata.get("retryable"),
        "after_seq": after_seq,
        "events": [event.to_dict() for event in events],
    }


@router.get("/v1/runs/{run_id}/checkpoint")
async def get_run_checkpoint(run_id: str, auth: AuthContext = Depends(require_auth)):
    checkpoint = get_checkpoint_store().get(run_id)
    if checkpoint is None:
        raise HTTPException(status_code=404, detail="checkpoint not found")
    metadata = await get_run_recorder().get_metadata(run_id)
    if metadata is None or metadata["tenant_id"] != auth.tenant_id:
        raise HTTPException(status_code=403, detail="run does not belong to this tenant")
    return checkpoint.to_dict()


@router.get("/v1/runtime/recoverable")
async def list_recoverable_runs(auth: AuthContext = Depends(require_auth)):
    """List expired checkpoints for a tenant-scoped recovery worker."""
    checkpoints = get_checkpoint_store().list_recoverable()
    result = []
    recorder = get_run_recorder()
    for checkpoint in checkpoints:
        metadata = await recorder.get_metadata(checkpoint.run_id)
        if metadata and metadata.get("tenant_id") == auth.tenant_id:
            result.append(checkpoint.to_dict())
    return {"checkpoints": result}


@router.post("/v1/runs/{run_id}/resume")
async def resume_run(run_id: str, auth: AuthContext = Depends(require_auth)):
    """Claim an approved checkpoint for a continuation worker.

    The worker owns actual tool execution; this endpoint only performs the
    durable state transition and lease claim, so duplicate resume requests
    cannot execute the same invocation twice.
    """
    store = get_checkpoint_store()
    checkpoint = store.get(run_id)
    if checkpoint is None:
        raise HTTPException(status_code=404, detail="checkpoint not found")
    metadata = await get_run_recorder().get_metadata(run_id)
    if metadata is None or metadata["tenant_id"] != auth.tenant_id:
        raise HTTPException(status_code=403, detail="run does not belong to this tenant")
    if checkpoint.state != "approved":
        reason = (
            "executing outcome requires reconciliation"
            if checkpoint.state == "executing"
            else "checkpoint is not approved"
        )
        return {
            "accepted": False,
            "reason": reason,
            "checkpoint": checkpoint.to_dict(),
        }
    from app.runtime.continuation import ContinuationWorker
    from app.auth.middleware import tenant_context
    from app.context.request_context import set_request_context
    from app.registry.tool_registry import get_registered_tools

    worker = ContinuationWorker(store, f"api:{auth.user_id}")
    await get_idempotency_store().recover_run(run_id)
    tenant_context.set(auth.tenant_id)
    set_request_context(
        request_id=f"resume-{run_id}", run_id=run_id,
        session_id=checkpoint.payload.get("session_id", metadata["session_id"]),
    )

    async def execute(checkpoint):
        tool = get_registered_tools().get(checkpoint.tool_name)
        if tool is None:
            raise ValueError(f"checkpoint tool is not registered: {checkpoint.tool_name}")
        return await tool.ainvoke(checkpoint.payload.get("arguments", {}))

    updated = await worker.run_once_async(run_id, execute)
    if updated is None:
        return {"accepted": False, "reason": "lease held", "checkpoint": checkpoint.to_dict()}
    return {"accepted": True, "checkpoint": updated.to_dict()}


@router.post("/v1/runs/{run_id}/cancel")
async def cancel_run(
    run_id: str,
    auth: AuthContext = Depends(require_auth),
):
    """Request cooperative cancellation of an active root run."""
    recorder = get_run_recorder()
    metadata = await recorder.get_metadata(run_id)
    if metadata is None:
        raise HTTPException(status_code=404, detail="run not found")
    if metadata["tenant_id"] != auth.tenant_id:
        raise HTTPException(status_code=403, detail="run does not belong to this tenant")
    if metadata["status"] != RunStatus.RUNNING.value:
        return {"run_id": run_id, "accepted": False, "status": metadata["status"]}

    accepted = await get_run_cancellation_registry().request(run_id)
    return {
        "run_id": run_id,
        "accepted": accepted,
        "status": "cancellation_requested" if accepted else metadata["status"],
        "note": "cooperative cancel — honored between runtime events",
    }


@router.get("/v1/approvals/{approval_id}")
async def get_approval(approval_id: str, auth: AuthContext = Depends(require_auth)):
    approval = await get_approval_registry().get(approval_id)
    if approval is None:
        raise HTTPException(status_code=404, detail="approval not found")
    if approval.tenant_id != auth.tenant_id:
        raise HTTPException(status_code=403, detail="approval does not belong to this tenant")
    return approval.to_dict()


@router.post("/v1/approvals/{approval_id}/decision")
async def decide_approval(
    approval_id: str,
    decision: dict,
    auth: AuthContext = Depends(require_auth),
):
    status = decision.get("status")
    if status not in {"approved", "rejected"}:
        raise HTTPException(status_code=400, detail="status must be approved or rejected")
    approval = await get_approval_registry().get(approval_id)
    if approval is None:
        raise HTTPException(status_code=404, detail="approval not found")
    if approval.tenant_id != auth.tenant_id:
        raise HTTPException(status_code=403, detail="approval does not belong to this tenant")
    approval = await get_approval_registry().decide(
        approval_id, status=status, decided_by=auth.user_id
    )
    if approval.status == "approved":
        from app.runtime.checkpoint import get_checkpoint_store
        checkpoint = get_checkpoint_store().get(approval.run_id)
        if (
            checkpoint is not None
            and checkpoint.approval_id == approval.approval_id
            and checkpoint.state == "waiting_approval"
        ):
            get_checkpoint_store().transition(approval.run_id, state="approved")
    recorder = get_run_recorder()
    run_metadata = await recorder.get_metadata(approval.run_id)
    if run_metadata is not None and run_metadata.get("tenant_id") == auth.tenant_id:
        await recorder.record_event(
            approval.run_id,
            "approval_decided",
            {
                "approval_id": approval.approval_id,
                "status": approval.status,
                "decided_by": approval.decided_by,
            },
        )
    return approval.to_dict()


@router.post("/v1/sessions/{session_id}/children/{child_id}/cancel")
async def cancel_child(
    session_id: str,
    child_id: str,
    auth: AuthContext = Depends(require_auth),
):
    """Request cooperative cancellation of an in-flight subagent.

    The session_id MUST already be tenant-prefixed (matching the format
    chat_completions uses internally). The endpoint enforces tenant
    isolation by rejecting any session_id whose tenant prefix doesn't
    match the authenticated tenant — without this check, a user in
    tenant A could cancel work in tenant B by guessing session ids.

    Cancellation is cooperative: see ``app.agent.fleet_bus`` and
    ``app.agent.subagent._run_child``. A child blocked inside a long
    tool call will not honor the cancel until the tool returns. The
    typical fan-out / wrong-path-interception scenario decides between
    tool rounds, where cancel is snappy (next ``astream_events``
    iteration).

    Returns 202 even when the session/child is unknown — that's a race,
    not an error: the child may have already finished by the time the
    cancel arrives. The client can poll ``/v1/sessions/{id}/status``
    (TODO) to learn the actual outcome.
    """
    # Tenant isolation — session_id format is "{tenant}:{user_session}".
    expected_prefix = f"{auth.tenant_id}:"
    if not session_id.startswith(expected_prefix):
        raise HTTPException(
            status_code=403,
            detail="session does not belong to this tenant",
        )

    accepted = fleet_request_cancel(
        root_session_id=session_id,
        child_session_id=child_id,
    )
    return {
        "session_id": session_id,
        "child_session_id": child_id,
        "accepted": accepted,
        "note": "cooperative cancel — honored between LangGraph events"
                if accepted
                else "session not found (child may have already completed)",
    }
