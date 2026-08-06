from langchain_core.messages import AIMessage, HumanMessage

from app.agent.tool_contract import missing_tool_requirement, required_tools, required_tools_for_agent


def test_explicit_run_requires_code_tool():
    assert required_tools("run python to calculate 2 + 2") == ("code_run",)


def test_memory_write_requires_memory_set():
    assert missing_tool_requirement([HumanMessage(content="remember my name is Hao")]) == ("memory_set",)


def test_observed_required_tool_is_not_missing():
    messages = [
        HumanMessage(content="run python to calculate 2 + 2"),
        AIMessage(content="", tool_calls=[{"name": "code_run", "args": {}, "id": "call-1", "type": "tool_call"}]),
    ]
    assert missing_tool_requirement(messages) == ()


def test_customer_qa_knowledge_question_requires_rag_only_when_available():
    assert required_tools_for_agent("What is the return policy?", {"rag_search", "memory_search"}) == ("rag_search",)
    assert required_tools_for_agent("What is the return policy?", {"file_read"}) == ()
