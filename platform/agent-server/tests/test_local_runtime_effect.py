from __future__ import annotations

import hashlib

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool

from app.agent.agent_graph.builder import build_agent_graph
from app.agent.agent_graph.nodes.context import _build_llm_view
from app.agent.agent_graph.state import AgentState


class ReadThenAnswerModel:
    def __init__(self) -> None:
        self.call_count = 0

    def bind_tools(self, _tools):
        return self

    async def ainvoke(self, messages):
        self.call_count += 1
        if any(isinstance(message, ToolMessage) for message in messages):
            return AIMessage(content="README inspected")
        return AIMessage(
            content="",
            tool_calls=[
                {
                    "id": "model-file-read",
                    "name": "file_read",
                    "args": {"path": "README.md"},
                }
            ],
        )


class BurstThenFinalizeModel:
    def __init__(self) -> None:
        self.bound_call_count = 0
        self.finalize_call_count = 0

    def bind_tools(self, _tools):
        parent = self

        class BoundModel:
            async def ainvoke(self, messages):
                parent.bound_call_count += 1
                call_count = 5 if parent.bound_call_count == 1 else 8
                return AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": f"burst-{parent.bound_call_count}-{index}",
                            "name": "inspect_item",
                            "args": {"name": f"item-{parent.bound_call_count}-{index}"},
                        }
                        for index in range(call_count)
                    ],
                )

        return BoundModel()

    async def ainvoke(self, messages):
        self.finalize_call_count += 1
        assert "RUNTIME FINALIZATION" in str(messages[0].content)
        return AIMessage(content="Completed from the collected evidence.")


@tool
def file_read(path: str) -> str:
    """Read a file from the test workspace."""
    assert path == "README.md"
    return "project overview"


async def _run_read_task(*, direct_routing: bool):
    model = ReadThenAnswerModel()
    graph = build_agent_graph(
        model=model,
        tools=[file_read],
        system_prompt="stable system prompt",
        direct_tool_routing_enabled=direct_routing,
    )
    result = await graph.ainvoke(
        {"messages": [HumanMessage(content="read README.md")]}
    )
    return model.call_count, result["messages"][-1].content


@pytest.mark.asyncio
async def test_direct_routing_ab_removes_one_actor_call_without_changing_answer():
    calls_without_routing, answer_without_routing = await _run_read_task(
        direct_routing=False
    )
    calls_with_routing, answer_with_routing = await _run_read_task(
        direct_routing=True
    )

    assert calls_without_routing == 2
    assert calls_with_routing == 1
    assert answer_with_routing == answer_without_routing == "README inspected"


@pytest.mark.asyncio
async def test_tool_burst_uses_remaining_budget_then_finalizes_without_tools():
    executions = []

    @tool
    def inspect_item(name: str) -> str:
        """Inspect one synthetic item."""
        executions.append(name)
        return f"inspected {name}"

    model = BurstThenFinalizeModel()
    graph = build_agent_graph(
        model=model,
        tools=[inspect_item],
        system_prompt="stable system prompt",
        max_steps=12,
        max_context_chars=20_000,
        direct_tool_routing_enabled=False,
    )

    result = await graph.ainvoke(
        {"messages": [HumanMessage(content="perform the bounded task")]}
    )

    assert len(executions) == 12
    assert model.bound_call_count == 2
    assert model.finalize_call_count == 1
    assert result["messages"][-1].content == "Completed from the collected evidence."


def test_rolling_context_keeps_tool_result_and_stable_prefix_for_follow_up():
    initial_messages = [
        HumanMessage(content="read README.md"),
        AIMessage(
            content="",
            tool_calls=[
                {
                    "id": "read-1",
                    "name": "file_read",
                    "args": {"path": "README.md"},
                }
            ],
        ),
        ToolMessage(
            content="project overview",
            tool_call_id="read-1",
            name="file_read",
        ),
        AIMessage(content="README inspected"),
    ]
    first = _build_llm_view(
        AgentState(messages=initial_messages),
        "stable system prompt",
        max_context_chars=20_000,
    )
    second = _build_llm_view(
        AgentState(
            messages=initial_messages
            + [HumanMessage(content="what were the main components?")]
        ),
        "stable system prompt",
        max_context_chars=20_000,
    )

    first_prefix = hashlib.sha256(first.messages[0].content.encode()).hexdigest()
    second_prefix = hashlib.sha256(second.messages[0].content.encode()).hexdigest()

    assert first_prefix == second_prefix
    assert second.messages[1 : 1 + len(initial_messages)] == initial_messages
    assert any(
        isinstance(message, ToolMessage) and message.content == "project overview"
        for message in second.messages
    )
    assert second.compression_summary == ""