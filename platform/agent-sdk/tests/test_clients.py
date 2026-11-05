"""Behavior contracts for SDK clients used by production-loaded plugins."""

import asyncio

import pytest

from agent_sdk.knowledge import KnowledgeBaseClient, KnowledgeUnavailableError
from agent_sdk.memory import MemoryClient, MemoryUnavailableError


class _SearchItem:
    content = "authentication flow"
    file_path = "docs/auth.md"
    name = "Authentication"
    score = 0.9


class _Retriever:
    async def search(self, query: str, top_k: int = 5):
        assert query == "auth"
        assert top_k == 3
        return [_SearchItem()]


def test_knowledge_client_returns_typed_results() -> None:
    client = KnowledgeBaseClient(retriever=_Retriever())

    results = asyncio.run(client.search("auth", top_k=3))

    assert [(item.file_path, item.score) for item in results] == [
        ("docs/auth.md", 0.9)
    ]


def test_knowledge_client_fails_explicitly_without_retriever() -> None:
    client = KnowledgeBaseClient()

    with pytest.raises(KnowledgeUnavailableError, match="retriever"):
        asyncio.run(client.search("auth"))


def test_memory_client_uses_transport_and_decodes_bridge_results() -> None:
    calls: list[tuple[str, dict]] = []

    async def call_tool(name: str, arguments: dict) -> str:
        calls.append((name, arguments))
        if name == "memory_search":
            return 'Found results:\n[{"key":"k","content":"value","score":0.8}]'
        if name == "memory_get":
            return '{"key":"k","content":"value"}'
        if name == "memory_set":
            return "Memory saved"
        raise AssertionError(name)

    client = MemoryClient(tool_caller=call_tool)

    assert asyncio.run(client.search("value", top_k=2)) == [
        {"key": "k", "content": "value", "score": 0.8}
    ]
    assert asyncio.run(client.get("k")) == "value"
    assert asyncio.run(client.set("k", "value")) == "Memory saved"
    assert calls == [
        ("memory_search", {"query": "value", "limit": 2}),
        ("memory_get", {"key": "k"}),
        ("memory_set", {"key": "k", "content": "value"}),
    ]


def test_memory_client_surfaces_backend_failure() -> None:
    async def call_tool(_name: str, _arguments: dict) -> str:
        return "Cannot connect to memory backend"

    client = MemoryClient(tool_caller=call_tool)

    with pytest.raises(MemoryUnavailableError, match="Cannot connect"):
        asyncio.run(client.search("value"))
