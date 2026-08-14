"""Vertical contract tests for context state, model view, and checkpoints."""

from pathlib import Path
from unittest.mock import patch

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langgraph.checkpoint.memory import MemorySaver

from app.agent.agent_graph.builder import build_agent_graph
from app.agent.compressor import InvestigationState, estimate_chars
from app.config import settings

AGENT_SERVER_ROOT = Path(__file__).resolve().parents[1]
PLATFORM_ROOT = AGENT_SERVER_ROOT.parent
ACTIVE_ARCHITECTURE_DOCS = (
    PLATFORM_ROOT / "docs" / "RUNTIME-REFACTOR-ROADMAP.md",
    PLATFORM_ROOT
    / "eval-harness"
    / "scenarios"
    / "context_compression"
    / "README.md",
    AGENT_SERVER_ROOT / "README.md",
)


class CapturingModel:
    def __init__(self) -> None:
        self.calls: list[list[BaseMessage]] = []

    async def ainvoke(self, messages: list[BaseMessage]) -> AIMessage:
        self.calls.append(list(messages))
        return AIMessage(content="done")


@pytest.mark.asyncio
async def test_model_view_is_bounded_while_checkpoint_remains_lossless():
    model = CapturingModel()
    checkpointer = MemorySaver()
    graph = build_agent_graph(
        model=model,
        tools=[],
        system_prompt="system",
        checkpointer=checkpointer,
    )
    original_messages: list[BaseMessage] = [
        HumanMessage(id="goal", content="find the root cause")
    ]
    original_messages.extend(
        AIMessage(id=f"history-{index}", content=f"history-{index}:" + "x" * 400)
        for index in range(12)
    )
    original_messages.append(HumanMessage(id="latest", content="continue"))
    original_contents = {
        message.id: str(message.content) for message in original_messages
    }
    config = {"configurable": {"thread_id": "context-contract"}}

    with patch.object(settings, "max_context_chars", 3000):
        await graph.ainvoke(
            {
                "messages": original_messages,
                "workspace_context": "workspace",
                "skills_catalog": "skills",
                "investigation": InvestigationState(
                    confirmed_facts=["root cause confirmed"]
                ),
            },
            config,
        )
        await graph.ainvoke(
            {"messages": [HumanMessage(id="follow-up", content="continue again")]},
            config,
        )

    assert len(model.calls) == 2
    for model_view in model.calls:
        assert estimate_chars(model_view) <= 3000
        facts = [
            message
            for message in model_view
            if "root cause confirmed" in str(message.content)
        ]
        assert len(facts) == 1

    persisted = graph.get_state(config).values["messages"]
    persisted_by_id = {message.id: str(message.content) for message in persisted}
    assert list(persisted_by_id)[: len(original_messages)] == [
        message.id for message in original_messages
    ]
    assert all(
        persisted_by_id[message_id] == content
        for message_id, content in original_contents.items()
    )
    assert "follow-up" in persisted_by_id
    assert all(message.id != "investigation-state-view" for message in persisted)


@pytest.mark.parametrize("document", ACTIVE_ARCHITECTURE_DOCS)
def test_active_architecture_docs_match_context_contract(document: Path):
    content = document.read_text(encoding="utf-8")

    assert "compress_history" not in content
    assert "context_compressor" not in content
    assert "- `investigation_summary`" not in content
    assert "investigation_summary  ← structured facts" not in content
    assert "_build_llm_view" in content