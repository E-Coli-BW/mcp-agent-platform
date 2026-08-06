from typing import Annotated

import pytest
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from typing_extensions import TypedDict


class _State(TypedDict):
    messages: Annotated[list, add_messages]
    visits: int


def _build_graph(checkpointer):
    graph = StateGraph(_State)

    def node(state):
        return {"messages": [HumanMessage(content=f"visit-{state.get('visits', 0) + 1}")], "visits": state.get("visits", 0) + 1}

    graph.add_node("step", node)
    graph.add_edge(START, "step")
    graph.add_edge("step", END)
    return graph.compile(checkpointer=checkpointer)


def test_langgraph_thread_state_reattaches_after_fresh_saver(tmp_path):
    database = str(tmp_path / "langgraph.sqlite3")
    config = {"configurable": {"thread_id": "session-reattach"}}

    with SqliteSaver.from_conn_string(database) as first_saver:
        first_saver.setup()
        first = _build_graph(first_saver)
        first.invoke({"messages": [HumanMessage(content="start")], "visits": 0}, config)

    with SqliteSaver.from_conn_string(database) as second_saver:
        second_saver.setup()
        second = _build_graph(second_saver)
        state = second.get_state(config)
        assert state.values["visits"] == 1
        second.invoke({"messages": [], "visits": 1}, config)
        assert second.get_state(config).values["visits"] == 2
