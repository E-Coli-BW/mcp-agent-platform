from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.messages import HumanMessage

from app.agent.engine import AgentResumeRequest, AgentTurnRequest
from app.agent.engine.langgraph import LangGraphAgentEngine


def test_platform_layers_do_not_import_concrete_agent_graphs():
    app_dir = Path(__file__).parents[1] / "app"
    violations = []

    for package_name in ("api", "runtime"):
        for module_path in (app_dir / package_name).rglob("*.py"):
            tree = ast.parse(module_path.read_text())
            imported_modules = []
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported_modules.extend(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported_modules.append(node.module)
            for module_name in imported_modules:
                if module_name == "app.agent.graph" or module_name.startswith(
                    "app.agent.graph_"
                ):
                    violations.append(
                        f"{module_path.relative_to(app_dir)} imports {module_name}"
                    )

    assert violations == []


def test_run_coordinator_has_no_wire_or_optional_observer_imports():
    app_dir = Path(__file__).parents[1] / "app"
    runner_path = app_dir / "runtime" / "runner.py"
    tree = ast.parse(runner_path.read_text())
    imported_modules = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.append(node.module)

    forbidden = (
        "fastapi",
        "app.api.openai_sse",
        "app.events.kafka_producer",
        "app.rag",
        "app.feedback",
        "app.usage",
    )
    assert not [
        module
        for module in imported_modules
        if any(module == prefix or module.startswith(f"{prefix}.") for prefix in forbidden)
    ]


class FakeLangGraphAgent:
    def __init__(self) -> None:
        self.stream_state = None
        self.stream_config = None
        self.stream_version = None
        self.state_config = None
        self.invoke_input = object()
        self.invoke_config = None

    async def _events(self):
        yield {"event": "on_chat_model_end", "data": {"output": "done"}}

    def astream_events(self, state, *, config, version):
        self.stream_state = state
        self.stream_config = config
        self.stream_version = version
        return self._events()

    async def aget_state(self, config):
        self.state_config = config
        return SimpleNamespace(next=("call_llm",))

    async def ainvoke(self, input_value, config):
        self.invoke_input = input_value
        self.invoke_config = config
        return {"messages": ["completed"]}


@pytest.mark.asyncio
async def test_langgraph_engine_streams_turn_with_engine_request():
    agent = FakeLangGraphAgent()
    resolver_calls = []

    async def resolve_agent(model, **kwargs):
        resolver_calls.append((model, kwargs))
        return agent

    engine = LangGraphAgentEngine(resolve_agent)
    request = AgentTurnRequest(
        model="coding-agent",
        session_id="tenant-a:session-1",
        tenant_id="tenant-a",
        user_input="inspect the failing test",
        recursion_limit=48,
        temperature=0.0,
        prompt_version="v2",
    )

    event_stream = await engine.stream_turn(request)

    assert [event async for event in event_stream] == [
        {"event": "on_chat_model_end", "data": {"output": "done"}}
    ]
    assert resolver_calls == [
        (
            "coding-agent",
            {
                "temperature": 0.0,
                "tenant_id": "tenant-a",
                "session_id": "tenant-a:session-1",
                "prompt_version": "v2",
            },
        )
    ]
    assert isinstance(agent.stream_state["messages"][0], HumanMessage)
    assert agent.stream_state["messages"][0].content == "inspect the failing test"
    assert agent.stream_config == {
        "configurable": {"thread_id": "tenant-a:session-1"},
        "recursion_limit": 48,
    }
    assert agent.stream_version == "v2"


@pytest.mark.asyncio
async def test_langgraph_engine_inspects_and_resumes_pending_graph():
    agent = FakeLangGraphAgent()

    async def resolve_agent(_model, **_kwargs):
        return agent

    engine = LangGraphAgentEngine(resolve_agent)
    request = AgentResumeRequest(
        model="coding-agent",
        session_id="tenant-a:session-1",
        tenant_id="tenant-a",
        recursion_limit=48,
        temperature=0.0,
        prompt_version="v2",
    )

    assert await engine.pending_nodes(request) == ("call_llm",)
    assert await engine.resume(request) == {"messages": ["completed"]}
    expected_config = {
        "configurable": {"thread_id": "tenant-a:session-1"},
        "recursion_limit": 48,
    }
    assert agent.state_config == expected_config
    assert agent.invoke_input is None
    assert agent.invoke_config == expected_config