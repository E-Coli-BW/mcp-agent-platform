from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.agent.tool_contract import (
    missing_tool_requirement,
    required_tools,
    required_tools_for_agent,
)
from app.artifacts import VerifiedArtifact


def test_explicit_run_requires_code_tool():
    assert required_tools("run python to calculate 2 + 2") == ("code_run",)


def test_project_read_requires_search_and_primary_source_read():
    prompt = "read this project and tell me the state transitions"

    assert required_tools(prompt) == ("file_search", "file_read")


def test_project_read_reports_only_unobserved_evidence_tools():
    messages = [
        HumanMessage(content="read this project and explain the workflow"),
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "file_search",
                    "args": {"query": "workflow"},
                    "id": "call-1",
                    "type": "tool_call",
                }
            ],
        ),
    ]

    assert missing_tool_requirement(messages) == ("file_read",)


def test_generic_project_read_falls_back_after_search_and_read():
    messages = [
        HumanMessage(content="read this project and explain the workflow"),
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "file_search",
                    "args": {"query": "workflow"},
                    "id": "search-1",
                    "type": "tool_call",
                },
                {
                    "name": "file_read",
                    "args": {"path": "workflow.py"},
                    "id": "read-1",
                    "type": "tool_call",
                },
            ],
        ),
        ToolMessage(content="workflow source", tool_call_id="read-1", name="file_read"),
    ]

    assert missing_tool_requirement(messages) == ()


def test_state_transition_read_requires_entry_edge_and_terminal_evidence():
    messages = [
        HumanMessage(content="read this project and explain the state transitions"),
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "file_search",
                    "args": {"query": "StartAt|Next|End"},
                    "id": "search-1",
                    "type": "tool_call",
                }
            ],
        ),
        ToolMessage(
            content=(
                "template.yml:450: Type: AWS::StepFunctions::StateMachine\n"
                "template.yml:457: StartAt: Core Workflow"
            ),
            tool_call_id="search-1",
            name="file_search",
        ),
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "file_read",
                    "args": {"path": "template.yml", "start_line": 457, "end_line": 463},
                    "id": "read-1",
                    "type": "tool_call",
                }
            ],
        ),
        ToolMessage(
            content='StartAt: "Core Workflow"',
            tool_call_id="read-1",
            name="file_read",
        ),
        HumanMessage(content="🚨 STOP. Required tool(s) for this explicit request: file_read."),
    ]

    assert missing_tool_requirement(messages) == ("file_read",)

    artifact = VerifiedArtifact(
        plugin_id="aws_sfn",
        artifact_type="workflow_transitions",
        source_path="template.yml",
        content=(
            "- Machine StartAt -> Core Workflow\n"
            "- Core Workflow: Next -> Generate response\n"
            "- Generate response: End"
        ),
    )
    messages.extend(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "file_read",
                        "args": {"path": "template.yml"},
                        "id": "read-2",
                        "type": "tool_call",
                    }
                ],
            ),
            ToolMessage(
                content=artifact.to_tool_block(),
                tool_call_id="read-2",
                name="file_read",
            ),
        ]
    )

    assert missing_tool_requirement(messages) == ()


def test_memory_write_requires_memory_set():
    assert missing_tool_requirement([HumanMessage(content="remember my name is Hao")]) == (
        "memory_set",
    )


def test_observed_required_tool_is_not_missing():
    messages = [
        HumanMessage(content="run python to calculate 2 + 2"),
        AIMessage(
            content="",
            tool_calls=[{"name": "code_run", "args": {}, "id": "call-1", "type": "tool_call"}],
        ),
    ]
    assert missing_tool_requirement(messages) == ()


def test_customer_qa_knowledge_question_requires_rag_only_when_available():
    assert required_tools_for_agent(
        "What is the return policy?", {"rag_search", "memory_search"}
    ) == ("rag_search",)
    assert required_tools_for_agent("What is the return policy?", {"file_read"}) == ()
