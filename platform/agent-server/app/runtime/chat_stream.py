"""Runtime orchestration for OpenAI-compatible chat streaming."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import AsyncIterator
from typing import Any

from fastapi import Request
from langchain_core.messages import HumanMessage

from app.agent.graph import get_agent
from app.agent.prompts import resolve_system_prompt
from app.api import openai_sse
from app.api.chat_models import ChatMessage
from app.config import settings
from app.context.request_context import get_request_context, set_prompt_version
from app.events.model_provenance import (
    build_feature_flags_snapshot,
    current_trace_id,
    infer_provider,
    make_model_call_event,
)
from app.runtime import RunStatus, get_run_recorder
from app.runtime.agent_event_fan_in import AgentEventFanIn
from app.runtime.cancellation import get_run_cancellation_registry
from app.runtime.policy import get_policy_evaluator
from app.runtime.tool_authorization import (
    reset_approval_notifier,
    set_approval_notifier,
)
from app.runtime.tool_invocation import make_tool_invocation_envelope
from app.store.conversation import Message, get_conversation_store
from app.usage import RequestUsage, estimate_tokens, get_usage_tracker

_FinalResponseBuffer = openai_sse.FinalResponseBuffer
_ToolEventTracker = openai_sse.ToolEventTracker
_is_actor_model_event = openai_sse.is_actor_model_event
_make_child_event = openai_sse.make_child_event
_make_chunk = openai_sse.make_chunk
_make_status_event = openai_sse.make_status_event
_make_tool_event = openai_sse.make_tool_event
_verified_artifact_answer_from_chain_event = (
    openai_sse.verified_artifact_answer_from_chain_event
)

logger = logging.getLogger("agent.chat")


async def stream_agent_response(
    messages: list[ChatMessage],
    model: str,
    session_id: str,
    tenant_id: str = "default",
    temperature: float | None = None,
    max_tokens: int | None = None,
    prompt_version: str | None = None,
    http_request: Request | None = None,
) -> AsyncIterator[str | dict[str, Any]]:
    """Execute one chat turn and translate runtime events into SSE payloads."""
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
    effective_temperature = (
        temperature if temperature is not None else settings.default_temperature
    )
    run_recorder = get_run_recorder()

    user_input = messages[-1].content if messages else ""
    await store.append(session_id, Message(role="user", content=user_input))

    input_preview = (
        user_input[:120] + "..." if len(user_input) > 120 else user_input
    )
    logger.info(
        "Received user message [session=%s, model=%s]: %s",
        session_id,
        model,
        input_preview,
    )

    config = {
        "configurable": {"thread_id": session_id},
        "recursion_limit": settings.max_agent_steps * 4,
    }
    full_response = ""
    lock_acquired = False
    client_disconnected = False
    cancel_requested = False
    fan_in: AgentEventFanIn | None = None
    approval_notifier_token = None

    try:
        usage = RequestUsage(model=model)
        tool_count = 0
        tool_events = _ToolEventTracker()
        response_buffer = _FinalResponseBuffer()
        start_time = time.time()
        model_call_seq = 0
        active_model_call: dict[str, Any] | None = None

        last_rag_query: str | None = None
        last_rag_chunks: list[Any] = []
        retrieval_trace_ids: list[str] = []
        file_reads_after_rag: list[str] = []
        tools_since_rag = 0

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

        test_delay_ms = int(os.environ.get("AGENT_TEST_DELAY_MS", "0"))
        if test_delay_ms > 0:
            await asyncio.sleep(test_delay_ms / 1000)

        from app.middleware.session_lane import (
            release_session_lock,
            session_lock_held,
            wait_for_session_lock,
        )

        if await session_lock_held(session_id):
            yield _make_status_event(
                "queued",
                request_id=req_ctx.request_id,
                run_id=req_ctx.run_id,
                message="Your message is queued behind the active turn",
            )

        if not await wait_for_session_lock(session_id, max_wait=900):
            await run_recorder.finish_run(
                req_ctx.run_id,
                RunStatus.FAILED,
                {
                    "request_id": req_ctx.request_id,
                    "session_id": session_id,
                    "terminal_reason": "session_lane_timeout",
                    "retryable": True,
                },
            )
            yield _make_chunk(
                "⚠️ Another request for this session is still processing. Please wait.",
                model,
                finish_reason="stop",
            )
            yield "[DONE]"
            return
        lock_acquired = True

        async def agent_events() -> AsyncIterator[dict[str, Any]]:
            async for event in agent.astream_events(
                {"messages": [HumanMessage(content=user_input)]},
                config=config,
                version="v2",
            ):
                yield event

        fan_in = AgentEventFanIn(
            agent_events=agent_events(),
            root_session_id=session_id,
        )

        async def notify_approval(approval: Any) -> None:
            await fan_in.emit("approval_required", approval)

        approval_notifier_token = set_approval_notifier(notify_approval)

        try:
            await fan_in.start()
            heartbeat_interval = 15
            while True:
                if http_request is not None and await http_request.is_disconnected():
                    logger.info("Client disconnected [session=%s]", session_id)
                    client_disconnected = True
                    await run_recorder.record_event(
                        req_ctx.run_id,
                        "client_disconnected",
                        {"session_id": session_id},
                    )
                    await fan_in.close()
                    break

                if await cancellation.is_requested(req_ctx.run_id):
                    logger.info("Root run cancellation requested [run=%s]", req_ctx.run_id)
                    cancel_requested = True
                    await run_recorder.record_event(
                        req_ctx.run_id,
                        "cancel_requested",
                        {
                            "request_id": req_ctx.request_id,
                            "session_id": session_id,
                        },
                    )
                    await fan_in.close()
                    break

                try:
                    source, event = await fan_in.next_event(timeout=heartbeat_interval)
                except TimeoutError:
                    yield ": keepalive\n"
                    continue
                except StopAsyncIteration:
                    break

                if source == "agent_error":
                    raise event

                if source == "approval_required":
                    yield {
                        "event": "approval_required",
                        "data": json.dumps(event.to_dict()),
                    }
                    continue

                if source == "bus":
                    yield _make_child_event(event)
                    continue

                kind = event.get("event", "")
                artifact_answer = _verified_artifact_answer_from_chain_event(event)
                if artifact_answer:
                    response_buffer.start_call()
                    response_buffer.append(artifact_answer)

                if kind == "on_chat_model_start":
                    is_actor_model = _is_actor_model_event(event)
                    if is_actor_model:
                        response_buffer.start_call()
                    model_input = event.get("data", {}).get("input", {})
                    messages_for_llm = (
                        model_input.get("messages", [])
                        if isinstance(model_input, dict)
                        else []
                    )
                    if messages_for_llm and isinstance(messages_for_llm[0], list):
                        messages_for_llm = messages_for_llm[0]
                    call_prompt_tokens = 0
                    for message in messages_for_llm:
                        content = getattr(message, "content", None)
                        if isinstance(content, str):
                            token_count = estimate_tokens(content)
                            usage.prompt_tokens += token_count
                            call_prompt_tokens += token_count
                        elif isinstance(content, list):
                            for part in content:
                                if isinstance(part, dict) and isinstance(part.get("text"), str):
                                    token_count = estimate_tokens(part["text"])
                                    usage.prompt_tokens += token_count
                                    call_prompt_tokens += token_count

                    model_call_seq += 1
                    active_model_call = {
                        "seq": model_call_seq,
                        "started_at": time.monotonic(),
                        "prompt_tokens": call_prompt_tokens,
                        "completion_tokens": 0,
                        "is_actor": is_actor_model,
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

                if kind == "on_chat_model_stream":
                    chunk = event.get("data", {}).get("chunk")
                    if chunk and hasattr(chunk, "content") and chunk.content:
                        if _is_actor_model_event(event):
                            response_buffer.append(chunk.content)
                        token_count = estimate_tokens(chunk.content)
                        usage.completion_tokens += token_count
                        if active_model_call is not None:
                            active_model_call["completion_tokens"] += token_count
                elif kind == "on_chat_model_end":
                    if active_model_call is not None:
                        duration_ms = int(
                            (time.monotonic() - active_model_call["started_at"]) * 1000
                        )
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

                elif kind == "on_tool_start":
                    tool_seq = tool_events.start(event)
                    if tool_seq is None:
                        continue
                    tool_name = event.get("name", "unknown")
                    tool_input = event.get("data", {}).get("input", {})
                    tool_count = tool_events.count
                    tool_envelope = make_tool_invocation_envelope(
                        run_id=req_ctx.run_id,
                        request_id=req_ctx.request_id,
                        session_id=session_id,
                        tenant_id=tenant_id,
                        step_seq=tool_seq,
                        tool_name=tool_name,
                        arguments=(
                            tool_input
                            if isinstance(tool_input, dict)
                            else {"input": tool_input}
                        ),
                    )
                    policy_decision = get_policy_evaluator().evaluate(tool_envelope)

                    from app.events.kafka_producer import get_event_producer

                    await get_event_producer().emit_tool_start(
                        session_id,
                        tool_name,
                        tool_input,
                        model,
                    )

                    if tool_name == "rag_search":
                        last_rag_query = tool_input.get("query", "")
                        last_rag_chunks = []
                        file_reads_after_rag = []
                        tools_since_rag = 0
                    elif (
                        tool_name == "file_read"
                        and last_rag_query
                        and tools_since_rag <= 3
                    ):
                        file_reads_after_rag.append(tool_input.get("path", ""))
                    if last_rag_query:
                        tools_since_rag += 1

                    tool_started_event = await run_recorder.record_event(
                        req_ctx.run_id,
                        "tool_started",
                        {
                            "seq": tool_seq,
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
                        seq=tool_seq,
                        event_id=tool_started_event.event_id,
                    )

                elif kind == "on_tool_end":
                    tool_seq = tool_events.end(event)
                    if tool_seq is None:
                        continue
                    tool_name = event.get("name", "unknown")
                    tool_output = str(event.get("data", {}).get("output", ""))[:500]
                    tool_input_end = event.get("data", {}).get("input", {})

                    if tool_name == "rag_search" and last_rag_query:
                        try:
                            from app.feedback.store import record_retrieval_trace
                            from app.rag.index.retriever import get_retriever_for_tenant

                            retriever = await get_retriever_for_tenant(
                                tenant_id,
                                collection=(
                                    get_request_context().knowledge_collection or None
                                ),
                            )
                            last_rag_chunks = await retriever.search(last_rag_query, top_k=5)
                            trace = record_retrieval_trace(
                                run_id=req_ctx.run_id,
                                session_id=session_id,
                                tenant_id=tenant_id,
                                agent=model,
                                query=last_rag_query,
                                chunks=[
                                    {
                                        "path": chunk.file_path,
                                        "name": chunk.name,
                                        "score": chunk.score,
                                        "start_line": chunk.start_line,
                                        "end_line": chunk.end_line,
                                    }
                                    for chunk in last_rag_chunks
                                ],
                                embedding_version=os.environ.get(
                                    "AGENT_EMBEDDING_MODEL", "mxbai-embed-large"
                                ),
                                reranker_version=os.environ.get(
                                    "AGENT_RERANKER", "learned"
                                ),
                                collection=(
                                    get_request_context().knowledge_collection
                                    or "default"
                                ),
                            )
                            retrieval_trace_ids.append(trace["trace_id"])
                        except Exception:
                            logger.debug(
                                "Failed to record retrieval trace [run=%s]",
                                req_ctx.run_id,
                                exc_info=True,
                            )
                            last_rag_chunks = []

                    tool_completed_event = await run_recorder.record_event(
                        req_ctx.run_id,
                        "tool_completed",
                        {
                            "seq": tool_seq,
                            "tool": tool_name,
                            "input": tool_input_end,
                            "output_preview": tool_output[:200],
                        },
                    )
                    yield _make_tool_event(
                        "end",
                        tool_name,
                        {"input": tool_input_end, "output": tool_output[:200]},
                        seq=tool_seq,
                        event_id=tool_completed_event.event_id,
                    )

                    from app.events.kafka_producer import get_event_producer

                    await get_event_producer().emit_tool_end(
                        session_id,
                        tool_name,
                        tool_output,
                    )
        finally:
            await fan_in.close()
            if approval_notifier_token is not None:
                reset_approval_notifier(approval_notifier_token)

        if active_model_call is not None:
            duration_ms = int(
                (time.monotonic() - active_model_call["started_at"]) * 1000
            )
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

        if not client_disconnected and not cancel_requested:
            full_response = response_buffer.text
            if full_response:
                yield _make_chunk(full_response, model)

        if full_response:
            await store.append(
                session_id,
                Message(role="assistant", content=full_response),
            )

        usage.tool_calls = tool_count
        usage.duration_ms = int((time.time() - start_time) * 1000)
        get_usage_tracker().record(usage)

        response_preview = (
            full_response[:120] + "..."
            if len(full_response) > 120
            else full_response
        )
        logger.info(
            "Agent response [session=%s, tools=%d, %dms]: %s",
            session_id,
            tool_count,
            usage.duration_ms,
            response_preview.replace("\n", " "),
        )

        from app.events.kafka_producer import get_event_producer

        await get_event_producer().emit_agent_response(
            session_id,
            model,
            usage.prompt_tokens + usage.completion_tokens,
            usage.duration_ms,
        )

        if last_rag_query and file_reads_after_rag:
            try:
                from app.rag.reranking.learned import get_learned_reranker

                reranker = get_learned_reranker()
                reranker.log_feedback(
                    last_rag_query,
                    last_rag_chunks,
                    file_reads_after_rag,
                )
                info = reranker.get_weights_info()
                if info["feedback_count"] > 0 and info["feedback_count"] % 50 == 0:
                    reranker.retrain()
            except Exception:
                logger.debug(
                    "Failed to record reranker feedback [run=%s]",
                    req_ctx.run_id,
                    exc_info=True,
                )

        run_finished_event = await run_recorder.finish_run(
            req_ctx.run_id,
            (
                RunStatus.CANCELLED
                if client_disconnected or cancel_requested
                else RunStatus.COMPLETED
            ),
            {
                "request_id": req_ctx.request_id,
                "session_id": session_id,
                "tool_count": tool_count,
                "duration_ms": usage.duration_ms,
                "prompt_tokens": usage.prompt_tokens,
                "completion_tokens": usage.completion_tokens,
                "terminal_reason": (
                    "client_disconnected"
                    if client_disconnected
                    else "cancel_requested"
                    if cancel_requested
                    else "completed"
                ),
                "retryable": bool(client_disconnected or cancel_requested),
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
        yield _make_chunk("", model, finish_reason="stop")
        yield "[DONE]"

    except Exception as exc:
        logger.exception("Agent turn failed [run=%s]", req_ctx.run_id)
        if active_model_call is not None:
            duration_ms = int(
                (time.monotonic() - active_model_call["started_at"]) * 1000
            )
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
                error_class=exc.__class__.__name__,
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
                    "error_class": exc.__class__.__name__,
                },
            )

        retryable = not isinstance(exc, (ValueError, PermissionError, KeyError))
        await run_recorder.finish_run(
            req_ctx.run_id,
            RunStatus.FAILED,
            {
                "request_id": req_ctx.request_id,
                "session_id": session_id,
                "error_class": exc.__class__.__name__,
                "error_message": str(exc),
                "terminal_reason": "agent_error",
                "retryable": retryable,
            },
        )
        yield _make_chunk(
            f"\n\n❌ Agent error: {exc}",
            model,
            finish_reason="stop",
        )
        yield "[DONE]"
    finally:
        if lock_acquired:
            await release_session_lock(session_id)
        await get_run_cancellation_registry().unregister(req_ctx.run_id)
