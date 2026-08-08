"""HTTP control-plane endpoints for runs, approvals, and child cancellation."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from app.agent.fleet_bus import request_cancel as fleet_request_cancel
from app.auth.middleware import AuthContext, require_auth, tenant_context
from app.context.request_context import set_request_context
from app.runtime import RunStatus, get_idempotency_store, get_run_recorder
from app.runtime.approval import get_approval_registry
from app.runtime.cancellation import get_run_cancellation_registry
from app.runtime.checkpoint import get_checkpoint_store
from app.runtime.continuation import ContinuationWorker

router = APIRouter(tags=["runtime"])
AUTH_DEPENDENCY = Depends(require_auth)


@router.get("/v1/runs/{run_id}/events")
async def get_run_events(
    run_id: str,
    after_seq: int = 0,
    auth: AuthContext = AUTH_DEPENDENCY,
) -> dict[str, Any]:
    """Return a tenant-owned run event timeline."""
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
async def get_run_checkpoint(
    run_id: str,
    auth: AuthContext = AUTH_DEPENDENCY,
) -> dict[str, Any]:
    checkpoint = get_checkpoint_store().get(run_id)
    if checkpoint is None:
        raise HTTPException(status_code=404, detail="checkpoint not found")
    metadata = await get_run_recorder().get_metadata(run_id)
    if metadata is None or metadata["tenant_id"] != auth.tenant_id:
        raise HTTPException(status_code=403, detail="run does not belong to this tenant")
    return checkpoint.to_dict()


@router.get("/v1/runtime/recoverable")
async def list_recoverable_runs(
    auth: AuthContext = AUTH_DEPENDENCY,
) -> dict[str, list[dict[str, Any]]]:
    """List recoverable checkpoints owned by the authenticated tenant."""
    recorder = get_run_recorder()
    result = []
    for checkpoint in get_checkpoint_store().list_recoverable():
        metadata = await recorder.get_metadata(checkpoint.run_id)
        if metadata and metadata.get("tenant_id") == auth.tenant_id:
            result.append(checkpoint.to_dict())
    return {"checkpoints": result}


@router.post("/v1/runs/{run_id}/resume")
async def resume_run(
    run_id: str,
    auth: AuthContext = AUTH_DEPENDENCY,
) -> dict[str, Any]:
    """Claim an approved checkpoint and execute it once under a lease."""
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
        return {"accepted": False, "reason": reason, "checkpoint": checkpoint.to_dict()}

    worker = ContinuationWorker(store, f"api:{auth.user_id}")
    await get_idempotency_store().recover_run(run_id)
    tenant_context.set(auth.tenant_id)
    set_request_context(
        request_id=f"resume-{run_id}",
        run_id=run_id,
        session_id=checkpoint.payload.get("session_id", metadata["session_id"]),
    )

    async def execute(active_checkpoint):
        from app.registry.tool_registry import get_registered_tools

        tool = get_registered_tools().get(active_checkpoint.tool_name)
        if tool is None:
            raise ValueError(
                f"checkpoint tool is not registered: {active_checkpoint.tool_name}"
            )
        return await tool.ainvoke(active_checkpoint.payload.get("arguments", {}))

    updated = await worker.run_once_async(run_id, execute)
    if updated is None:
        return {"accepted": False, "reason": "lease held", "checkpoint": checkpoint.to_dict()}
    return {"accepted": True, "checkpoint": updated.to_dict()}


@router.post("/v1/runs/{run_id}/cancel")
async def cancel_run(
    run_id: str,
    auth: AuthContext = AUTH_DEPENDENCY,
) -> dict[str, Any]:
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
async def get_approval(
    approval_id: str,
    auth: AuthContext = AUTH_DEPENDENCY,
) -> dict[str, Any]:
    approval = await get_approval_registry().get(approval_id)
    if approval is None:
        raise HTTPException(status_code=404, detail="approval not found")
    if approval.tenant_id != auth.tenant_id:
        raise HTTPException(status_code=403, detail="approval does not belong to this tenant")
    return approval.to_dict()


@router.post("/v1/approvals/{approval_id}/decision")
async def decide_approval(
    approval_id: str,
    decision: dict[str, Any],
    auth: AuthContext = AUTH_DEPENDENCY,
) -> dict[str, Any]:
    status = decision.get("status")
    if status not in {"approved", "rejected"}:
        raise HTTPException(status_code=400, detail="status must be approved or rejected")
    approval = await get_approval_registry().get(approval_id)
    if approval is None:
        raise HTTPException(status_code=404, detail="approval not found")
    if approval.tenant_id != auth.tenant_id:
        raise HTTPException(status_code=403, detail="approval does not belong to this tenant")
    approval = await get_approval_registry().decide(
        approval_id,
        status=status,
        decided_by=auth.user_id,
    )
    if approval.status == "approved":
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
    auth: AuthContext = AUTH_DEPENDENCY,
) -> dict[str, Any]:
    """Request cooperative cancellation of a tenant-owned in-flight child."""
    if not session_id.startswith(f"{auth.tenant_id}:"):
        raise HTTPException(status_code=403, detail="session does not belong to this tenant")

    accepted = fleet_request_cancel(
        root_session_id=session_id,
        child_session_id=child_id,
    )
    return {
        "session_id": session_id,
        "child_session_id": child_id,
        "accepted": accepted,
        "note": (
            "cooperative cancel — honored between LangGraph events"
            if accepted
            else "session not found (child may have already completed)"
        ),
    }