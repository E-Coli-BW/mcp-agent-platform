"""Execution-boundary policy and approval gate for LangChain tools."""

from __future__ import annotations

import asyncio
from contextvars import ContextVar, Token
from typing import Any, Awaitable, Callable
from weakref import WeakValueDictionary

from langchain_core.tools import BaseTool, StructuredTool

from app.auth.middleware import tenant_context
from app.context.request_context import get_request_context
from app.runtime.approval import ApprovalRegistry, ApprovalRequest, get_approval_registry
from app.runtime.checkpoint import SQLiteCheckpointStore, get_checkpoint_store
from app.runtime.policy import PolicyAction, RuntimePolicyEvaluator, get_policy_evaluator
from app.runtime.recorder import get_run_recorder
from app.runtime.tool_invocation import make_tool_invocation_envelope


ApprovalNotifier = Callable[[ApprovalRequest], Awaitable[None]]
_approval_notifier: ContextVar[ApprovalNotifier | None] = ContextVar(
    "approval_notifier", default=None
)
_run_approval_locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()


def set_approval_notifier(notifier: ApprovalNotifier) -> Token:
    return _approval_notifier.set(notifier)


def reset_approval_notifier(token: Token) -> None:
    _approval_notifier.reset(token)


def _approval_lock(run_id: str) -> asyncio.Lock:
    lock = _run_approval_locks.get(run_id)
    if lock is None:
        lock = asyncio.Lock()
        _run_approval_locks[run_id] = lock
    return lock


class ToolAuthorizationGate:
    """Evaluate and enforce tool policy before invoking the operation."""

    def __init__(
        self,
        *,
        evaluator: RuntimePolicyEvaluator | None = None,
        approval_registry: ApprovalRegistry | None = None,
        checkpoint_store: SQLiteCheckpointStore | None = None,
    ):
        self._evaluator = evaluator
        self._approval_registry = approval_registry
        self._checkpoint_store = checkpoint_store

    def evaluate(self, tool_name: str, arguments: dict[str, Any]):
        request = get_request_context()
        checkpoint = self._checkpoint_store_or_default().get(request.run_id)
        step_seq = checkpoint.step_seq + 1 if checkpoint is not None else 1
        envelope = make_tool_invocation_envelope(
            run_id=request.run_id or "untracked",
            request_id=request.request_id,
            session_id=request.session_id,
            tenant_id=tenant_context.get() or "default",
            step_seq=step_seq,
            tool_name=tool_name,
            arguments=arguments,
        )
        evaluator = self._evaluator or get_policy_evaluator()
        return envelope, evaluator.evaluate(envelope)

    async def invoke(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        operation: Callable[[], Awaitable[Any]],
        notify_approval: ApprovalNotifier | None = None,
    ) -> Any:
        envelope, decision = self.evaluate(tool_name, arguments)
        if decision.action not in {
            PolicyAction.ALLOW,
            PolicyAction.REQUIRE_APPROVAL,
        }:
            request = get_request_context()
            if request.run_id:
                await get_run_recorder().record_event(
                    request.run_id,
                    "policy_denied",
                    {
                        "tool": tool_name,
                        "invocation": envelope.to_event_payload(),
                        "policy_decision": decision.to_event_payload(),
                    },
                )
            raise PermissionError(
                f"Tool '{tool_name}' denied by runtime policy: {decision.reason}"
            )
        if decision.action != PolicyAction.REQUIRE_APPROVAL:
            return await operation()

        request = get_request_context()
        if not request.run_id:
            raise RuntimeError(
                f"Tool '{tool_name}' requires approval but no run context is active"
            )

        async with _approval_lock(request.run_id):
            return await self._invoke_with_approval(
                tool_name=tool_name,
                arguments=arguments,
                operation=operation,
                notify_approval=notify_approval,
            )

    async def _invoke_with_approval(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        operation: Callable[[], Awaitable[Any]],
        notify_approval: ApprovalNotifier | None,
    ) -> Any:
        request = get_request_context()
        envelope, decision = self.evaluate(tool_name, arguments)

        registry = self._approval_registry or get_approval_registry()
        checkpoints = self._checkpoint_store_or_default()
        approval = await registry.create(
            run_id=request.run_id,
            tenant_id=tenant_context.get() or "default",
            tool_name=tool_name,
            reason=decision.reason,
        )
        checkpoint = checkpoints.save_next(
            run_id=request.run_id,
            state="waiting_approval",
            tool_name=tool_name,
            approval_id=approval.approval_id,
            payload={
                "arguments": arguments,
                "session_id": request.session_id,
                "invocation": envelope.to_event_payload(),
                "policy_decision": decision.to_event_payload(),
            },
        )
        recorder = get_run_recorder()
        await recorder.record_event(
            request.run_id,
            "approval_required",
            {
                "approval_id": approval.approval_id,
                "tool": tool_name,
                "invocation": envelope.to_event_payload(),
                "policy_decision": decision.to_event_payload(),
            },
        )
        notifier = notify_approval or _approval_notifier.get()
        if notifier is not None:
            await notifier(approval)

        approval = await registry.wait_for_decision(approval.approval_id)
        status = approval.status if approval is not None else "missing"
        await recorder.record_event(
            request.run_id,
            "approval_decided",
            {"approval_id": checkpoint.approval_id, "status": status},
        )
        if status != "approved":
            terminal_state = "rejected" if status == "rejected" else "failed"
            checkpoints.transition(
                request.run_id,
                state=terminal_state,
                payload={"approval_status": status},
            )
            raise PermissionError(
                f"Approval {status} before tool '{tool_name}' could execute"
            )

        current = checkpoints.get(request.run_id)
        if current is None or current.approval_id != approval.approval_id:
            raise RuntimeError("approval checkpoint no longer matches tool invocation")
        if current.state == "waiting_approval":
            checkpoints.transition(request.run_id, state="approved")
        checkpoints.transition(request.run_id, state="executing")
        try:
            result = await operation()
        except Exception as exc:
            checkpoints.transition(
                request.run_id,
                state="failed",
                payload={
                    "error_class": exc.__class__.__name__,
                    "error_message": str(exc),
                },
            )
            raise
        checkpoints.transition(
            request.run_id,
            state="completed",
            payload={"result": str(result)},
        )
        return result

    def _checkpoint_store_or_default(self) -> SQLiteCheckpointStore:
        return self._checkpoint_store or get_checkpoint_store()


def wrap_tool_with_authorization(tool: BaseTool) -> BaseTool:
    """Wrap a tool without changing its public name, schema, or response format."""
    inner = tool
    gate = ToolAuthorizationGate()

    async def _wrapped_async(**kwargs):
        return await gate.invoke(
            tool_name=inner.name,
            arguments=kwargs,
            operation=lambda: inner.ainvoke(kwargs),
        )

    def _wrapped_sync(**kwargs):
        _, decision = gate.evaluate(inner.name, kwargs)
        if decision.action not in {
            PolicyAction.ALLOW,
            PolicyAction.REQUIRE_APPROVAL,
        }:
            raise PermissionError(
                f"Tool '{inner.name}' denied by runtime policy: {decision.reason}"
            )
        if decision.action == PolicyAction.REQUIRE_APPROVAL:
            raise RuntimeError("approval-gated tools require async graph invocation")
        return inner.invoke(kwargs)

    return StructuredTool.from_function(
        func=_wrapped_sync,
        coroutine=_wrapped_async,
        name=inner.name,
        description=inner.description,
        args_schema=inner.args_schema,
        response_format=getattr(inner, "response_format", "content"),
    )