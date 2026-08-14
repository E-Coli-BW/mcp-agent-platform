"""Resume a due model retry from durable LangGraph and runtime checkpoints."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from langchain_core.messages import AIMessage

from app.auth.middleware import tenant_context
from app.config import settings
from app.context.request_context import set_request_context
from app.runtime import RunStatus, get_run_recorder
from app.runtime.checkpoint import RunCheckpoint, SQLiteCheckpointStore


class ModelRetryReconciliationError(RuntimeError):
    """Durable runtime and graph state cannot be replayed automatically."""

    terminal_reason = "model_retry_reconciliation_required"
    retryable = False


@dataclass(frozen=True)
class ModelRetryResumeResult:
    response: str
    checkpoint: RunCheckpoint


def _assistant_text(messages: list[Any]) -> str:
    for message in reversed(messages):
        if not isinstance(message, AIMessage):
            continue
        content = message.content
        if isinstance(content, str) and content.strip():
            return content
        if isinstance(content, list):
            text = "".join(
                block.get("text", "")
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            )
            if text.strip():
                return text
    raise ModelRetryReconciliationError(
        "resumed graph completed without a final assistant response"
    )


async def resume_model_retry_run(
    *,
    run_id: str,
    checkpoint: RunCheckpoint,
    metadata: dict[str, Any],
    tenant_id: str,
    user_id: str,
    store: SQLiteCheckpointStore,
) -> ModelRetryResumeResult:
    """Resume the pending call_llm node after a claimed waiting checkpoint."""
    from app.agent.graph import get_agent
    from app.agent.subagent_context import init_root_context
    from app.middleware.session_lane import release_session_lock, wait_for_session_lock
    from app.registry.tool_registry import get_registered_tools
    from app.runtime.cancellation import get_run_cancellation_registry
    from app.store.conversation import Message, get_conversation_store

    payload = checkpoint.payload
    session_id = str(payload.get("session_id") or metadata["session_id"])
    model = str(metadata["model"])
    prompt_version = str(payload.get("prompt_version") or "")
    recorder = get_run_recorder()
    events = await recorder.list_events(run_id)
    started = next((event for event in events if event.event_type == "run_started"), None)
    temperature = started.payload.get("temperature") if started is not None else None

    tenant_context.set(tenant_id)
    set_request_context(
        request_id=f"resume-{run_id}",
        run_id=run_id,
        session_id=session_id,
    )
    init_root_context(
        root_session_id=session_id,
        allowed_tools=[name for name in get_registered_tools() if name != "spawn_subagent"],
    )

    if not await wait_for_session_lock(session_id, max_wait=1):
        raise ModelRetryReconciliationError(
            "another request currently owns the session lane"
        )

    cancellation = get_run_cancellation_registry()
    await cancellation.register(run_id)
    try:
        agent = await get_agent(
            model,
            temperature=temperature,
            tenant_id=tenant_id,
            session_id=session_id,
            prompt_version=prompt_version,
        )
        config = {
            "configurable": {"thread_id": session_id},
            "recursion_limit": settings.max_agent_steps * 4,
        }
        snapshot = await agent.aget_state(config)
        if "call_llm" not in snapshot.next:
            store.transition(
                run_id,
                state="reconciliation_required",
                payload={
                    "reason": "durable graph has no pending call_llm node",
                    "graph_next": list(snapshot.next),
                },
                clear_lease=True,
            )
            raise ModelRetryReconciliationError(
                f"durable graph cannot resume model retry (next={snapshot.next!r})"
            )

        await recorder.record_event(
            run_id,
            "model_retry_resume_started",
            {
                "checkpoint_step": checkpoint.step_seq,
                "attempts_failed": payload.get("attempts_failed", 0),
                "resumed_by": user_id,
            },
        )
        result = await agent.ainvoke(None, config)
        response = _assistant_text(list(result.get("messages", [])))
        completed = store.get(run_id)
        if completed is None or completed.state != "completed":
            raise ModelRetryReconciliationError(
                "resumed graph returned without completing its retry checkpoint"
            )
        await get_conversation_store().append(
            session_id,
            Message(role="assistant", content=response),
        )
        await recorder.record_event(
            run_id,
            "model_retry_resume_completed",
            {"checkpoint_step": completed.step_seq},
        )
        await recorder.finish_run(
            run_id,
            RunStatus.COMPLETED,
            {
                "request_id": f"resume-{run_id}",
                "session_id": session_id,
                "terminal_reason": "model_retry_recovered",
                "retryable": False,
            },
        )
        return ModelRetryResumeResult(response=response, checkpoint=completed)
    except Exception as error:
        current = store.get(run_id)
        if current is not None and current.state == "model_retry_executing":
            terminal_reason = getattr(error, "terminal_reason", "")
            next_state = (
                "reconciliation_required"
                if terminal_reason == "model_retry_fingerprint_mismatch"
                else "failed"
            )
            store.transition(
                run_id,
                state=next_state,
                payload={
                    "error_class": type(error).__name__,
                    "error_message": str(error),
                },
                clear_lease=True,
            )
        await recorder.finish_run(
            run_id,
            RunStatus.FAILED,
            {
                "request_id": f"resume-{run_id}",
                "session_id": session_id,
                "terminal_reason": getattr(error, "terminal_reason", "model_retry_resume_failed"),
                "retryable": bool(getattr(error, "retryable", False)),
                "error_class": type(error).__name__,
                "error_message": str(error),
            },
        )
        raise
    finally:
        await cancellation.unregister(run_id)
        await release_session_lock(session_id)