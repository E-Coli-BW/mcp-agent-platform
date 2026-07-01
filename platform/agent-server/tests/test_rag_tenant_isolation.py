"""Regression tests for C7 — cross-tenant RAG retriever isolation."""

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from app.auth.middleware import tenant_context
from app.rag.chunking.code import CodeChunk
from app.rag.index import indexer
from app.rag.index.retriever import (
    InMemoryRetriever,
    _retriever_cache,
    clear_tenant_cache,
    get_retriever_for_tenant,
)
from app.tools.rag_tool import rag_search


@pytest.fixture(autouse=True)
def isolate_retriever_cache(monkeypatch, tmp_path):
    """Keep each test isolated from the developer's real local RAG indexes."""
    monkeypatch.setenv("AGENT_RAG_BACKEND", "memory")
    monkeypatch.setattr(indexer, "BASE_INDEX_DIR", tmp_path / "rag-index")
    _retriever_cache.clear()
    clear_tenant_cache("alice")
    clear_tenant_cache("bob")
    yield
    clear_tenant_cache("alice")
    clear_tenant_cache("bob")
    _retriever_cache.clear()


def _make_chunk(name: str, file_name: str = "tenant.py") -> CodeChunk:
    return CodeChunk(
        content=f"def {name}():\n    return '{name}'\n",
        file_path=f"/{file_name}",
        language="python",
        chunk_type="function",
        name=name,
        start_line=1,
        end_line=2,
    )


def _write_tenant_index(tenant_id: str) -> None:
    retriever = InMemoryRetriever()
    retriever.load_index([_make_chunk(f"{tenant_id}_auth", f"{tenant_id}.py")], [[1.0, 0.0]])
    retriever.save_index(str(indexer.get_index_dir_for_tenant(tenant_id)))


@pytest.mark.asyncio
async def test_different_tenants_get_different_retrievers():
    alice = await get_retriever_for_tenant("alice")
    bob = await get_retriever_for_tenant("bob")

    assert alice is not bob


@pytest.mark.asyncio
async def test_same_tenant_two_calls_return_same_instance():
    first = await get_retriever_for_tenant("alice")
    second = await get_retriever_for_tenant("alice")

    assert first is second


@pytest.mark.asyncio
async def test_loaded_chunks_do_not_leak_across_tenants():
    alice = await get_retriever_for_tenant("alice")
    alice.chunks = [MagicMock(file_path="alice.py")]
    alice._indexed = True

    bob = await get_retriever_for_tenant("bob")

    assert bob.chunks == []


@pytest.mark.asyncio
async def test_rag_search_uses_per_tenant_retriever(monkeypatch):
    _write_tenant_index("alice")

    async def fake_embed_text(_query: str) -> list[float]:
        return [1.0, 0.0]

    reranker = MagicMock()
    reranker.rerank.side_effect = lambda _query, results, top_k: results[:top_k]

    with (
        patch("app.rag.index.retriever.embed_text", side_effect=fake_embed_text),
        patch("app.rag.reranking.learned.get_learned_reranker", return_value=reranker),
    ):
        alice_token = tenant_context.set("alice")
        try:
            alice_result = await rag_search.ainvoke({"query": "auth", "top_k": 1})
        finally:
            tenant_context.reset(alice_token)

        bob_token = tenant_context.set("bob")
        try:
            bob_result = await rag_search.ainvoke({"query": "auth", "top_k": 1})
        finally:
            tenant_context.reset(bob_token)

    assert "alice.py" in alice_result
    assert "No codebase indexed" in bob_result


@pytest.mark.asyncio
async def test_concurrent_tenant_requests_are_isolated():
    tenants = ["alice" if i % 2 == 0 else "bob" for i in range(10)]
    retrievers = await asyncio.gather(*(get_retriever_for_tenant(tenant) for tenant in tenants))

    alice = retrievers[0]
    bob = retrievers[1]

    assert alice is not bob
    assert all(retriever is alice for retriever in retrievers[::2])
    assert all(retriever is bob for retriever in retrievers[1::2])


@pytest.mark.asyncio
async def test_clear_tenant_cache_evicts_only_one_tenant():
    await get_retriever_for_tenant("alice")
    await get_retriever_for_tenant("bob")

    clear_tenant_cache("alice")

    assert not any(key.startswith("alice:") for key in _retriever_cache)
    assert any(key.startswith("bob:") for key in _retriever_cache)
