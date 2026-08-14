import asyncio
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import MemorySaver

from app.agent.agent_graph.builder import build_agent_graph
from app.auth.middleware import tenant_context
from app.context.request_context import set_request_context
from app.runtime.approval import ApprovalRegistry
from app.runtime.checkpoint import SQLiteCheckpointStore
from app.runtime.policy import PolicyAction, PolicyRule, RuntimePolicyEvaluator
from app.runtime.tool_authorization import (
    ToolAuthorizationGate,
    reset_approval_notifier,
    set_approval_notifier,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.asyncio
async def test_approved_tool_executes_only_after_decision(tmp_path):
    registry = ApprovalRegistry(str(tmp_path / "runtime.sqlite3"))
    checkpoints = SQLiteCheckpointStore(tmp_path / "runtime.sqlite3")
    gate = ToolAuthorizationGate(
        evaluator=RuntimePolicyEvaluator(
            rules=[
                PolicyRule(
                    tool_name="file_write",
                    action=PolicyAction.REQUIRE_APPROVAL,
                    reason="write requires approval",
                )
            ]
        ),
        approval_registry=registry,
        checkpoint_store=checkpoints,
    )
    tenant_context.set("tenant-a")
    set_request_context("request-1", "run-1", "tenant-a:session-1")
    approval_ready = asyncio.Event()
    approval_id = ""
    calls = []

    async def notify(approval):
        nonlocal approval_id
        approval_id = approval.approval_id
        approval_ready.set()

    async def operation():
        calls.append("executed")
        return "ok"

    task = asyncio.create_task(
        gate.invoke(
            tool_name="file_write",
            arguments={"path": "a.txt", "content": "hello"},
            operation=operation,
            notify_approval=notify,
        )
    )
    await asyncio.wait_for(approval_ready.wait(), timeout=1)

    assert calls == []
    assert checkpoints.get("run-1").state == "waiting_approval"

    await registry.decide(approval_id, status="approved", decided_by="user-1")
    assert await task == "ok"
    assert calls == ["executed"]
    assert checkpoints.get("run-1").state == "completed"


@pytest.mark.asyncio
async def test_denied_tool_never_executes(tmp_path):
    calls = []
    gate = ToolAuthorizationGate(
        evaluator=RuntimePolicyEvaluator(
            rules=[
                PolicyRule(
                    tool_name="code_shell",
                    action=PolicyAction.DENY,
                    reason="execution denied",
                )
            ]
        ),
        approval_registry=ApprovalRegistry(str(tmp_path / "runtime.sqlite3")),
        checkpoint_store=SQLiteCheckpointStore(tmp_path / "runtime.sqlite3"),
    )
    tenant_context.set("tenant-a")
    set_request_context("request-1", "run-1", "tenant-a:session-1")

    async def operation():
        calls.append("executed")
        return "ok"

    with pytest.raises(PermissionError, match="execution denied"):
        await gate.invoke(
            tool_name="code_shell",
            arguments={"command": "echo unsafe"},
            operation=operation,
        )

    assert calls == []


@pytest.mark.asyncio
async def test_parallel_approvals_are_serialized_per_run(tmp_path):
    registry = ApprovalRegistry(str(tmp_path / "runtime.sqlite3"))
    checkpoints = SQLiteCheckpointStore(tmp_path / "runtime.sqlite3")
    gate = ToolAuthorizationGate(
        evaluator=RuntimePolicyEvaluator(
            rules=[
                PolicyRule(
                    tool_name="file_write",
                    action=PolicyAction.REQUIRE_APPROVAL,
                    reason="write requires approval",
                )
            ]
        ),
        approval_registry=registry,
        checkpoint_store=checkpoints,
    )
    tenant_context.set("tenant-a")
    set_request_context("request-1", "run-parallel", "tenant-a:session-1")
    approvals = asyncio.Queue()
    calls = []

    async def notify(approval):
        await approvals.put(approval)

    async def invoke(path):
        async def operation():
            calls.append(path)
            return path

        return await gate.invoke(
            tool_name="file_write",
            arguments={"path": path},
            operation=operation,
            notify_approval=notify,
        )

    first_task = asyncio.create_task(invoke("first.txt"))
    second_task = asyncio.create_task(invoke("second.txt"))
    first_approval = await asyncio.wait_for(approvals.get(), timeout=1)
    assert approvals.empty()
    assert calls == []

    await registry.decide(
        first_approval.approval_id,
        status="approved",
        decided_by="user-1",
    )
    assert await first_task == "first.txt"
    second_approval = await asyncio.wait_for(approvals.get(), timeout=1)
    assert calls == ["first.txt"]

    await registry.decide(
        second_approval.approval_id,
        status="approved",
        decided_by="user-1",
    )
    assert await second_task == "second.txt"
    assert calls == ["first.txt", "second.txt"]
    assert checkpoints.get("run-parallel").step_seq == 2
    assert checkpoints.get("run-parallel").state == "completed"


@pytest.mark.asyncio
async def test_stategraph_toolnode_waits_for_approval_before_side_effect(
    tmp_path, monkeypatch
):
    registry = ApprovalRegistry(str(tmp_path / "runtime.sqlite3"))
    checkpoints = SQLiteCheckpointStore(tmp_path / "runtime.sqlite3")
    evaluator = RuntimePolicyEvaluator(
        rules=[
            PolicyRule(
                tool_name="file_write",
                action=PolicyAction.REQUIRE_APPROVAL,
                reason="write requires approval",
            )
        ]
    )
    monkeypatch.setattr(
        "app.runtime.tool_authorization.get_approval_registry",
        lambda: registry,
    )
    monkeypatch.setattr(
        "app.runtime.tool_authorization.get_checkpoint_store",
        lambda: checkpoints,
    )
    monkeypatch.setattr(
        "app.runtime.tool_authorization.get_policy_evaluator",
        lambda: evaluator,
    )
    calls = []

    @tool
    async def file_write(path: str, content: str) -> str:
        """Write a test file."""
        calls.append((path, content))
        return "written"

    class ToolCallingModel:
        def __init__(self):
            self.call_count = 0

        def bind_tools(self, _tools):
            return self

        async def ainvoke(self, _messages):
            self.call_count += 1
            if self.call_count == 1:
                return AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "call-1",
                            "name": "file_write",
                            "args": {"path": "a.txt", "content": "hello"},
                        }
                    ],
                )
            return AIMessage(content="done")

    graph = build_agent_graph(
        model=ToolCallingModel(),
        tools=[file_write],
        system_prompt="system",
        checkpointer=MemorySaver(),
    )
    tenant_context.set("tenant-a")
    set_request_context("request-1", "run-graph", "tenant-a:session-1")
    approval_ready = asyncio.Event()
    approval_id = ""

    async def notify(approval):
        nonlocal approval_id
        approval_id = approval.approval_id
        approval_ready.set()

    notifier_token = set_approval_notifier(notify)
    try:
        task = asyncio.create_task(
            graph.ainvoke(
                {"messages": []},
                {"configurable": {"thread_id": "approval-graph"}},
            )
        )
        await asyncio.wait_for(approval_ready.wait(), timeout=2)
        assert calls == []

        await registry.decide(
            approval_id,
            status="approved",
            decided_by="user-1",
        )
        await task
    finally:
        reset_approval_notifier(notifier_token)

    assert calls == [("a.txt", "hello")]
    assert checkpoints.get("run-graph").state == "completed"


def test_runtime_architecture_docs_describe_pre_execution_gate():
    roadmap = (
        REPOSITORY_ROOT / "platform" / "docs" / "RUNTIME-REFACTOR-ROADMAP.md"
    ).read_text(encoding="utf-8")
    readme = (
        REPOSITORY_ROOT / "platform" / "agent-server" / "README.md"
    ).read_text(encoding="utf-8")

    assert "wrap_tool_with_authorization()" in roadmap
    assert "does not enforce allow/deny/approval yet" not in roadmap
    assert "HITL / approval | Not implemented" not in roadmap
    assert "tool authorization gate" in readme