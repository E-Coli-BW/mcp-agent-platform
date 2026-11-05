from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytest.importorskip("tree_sitter_python")

from app.context.request_context import set_agent_contract
from app.tools.rag_tool import rag_search


@pytest.mark.asyncio
async def test_rag_search_uses_domain_collection_and_shows_source():
    set_agent_contract("customer-qa-agent", "customer-faq")
    retriever = SimpleNamespace(
        is_indexed=True,
        search=AsyncMock(return_value=[SimpleNamespace(
            file_path="returns-and-refunds.md",
            start_line=4,
            end_line=8,
            chunk_type="markdown",
            name="returns",
            language="markdown",
            content="Return information",
            score=0.9,
        )]),
    )
    with patch("app.tools.rag_tool.get_retriever_for_tenant", AsyncMock(return_value=retriever)):
        result = await rag_search.ainvoke({"query": "return policy"})

    assert "customer-faq" in result
    assert "returns-and-refunds.md:4-8" in result


@pytest.mark.asyncio
async def test_rag_search_uses_configured_candidate_pool(monkeypatch):
    from app.config import settings

    set_agent_contract("customer-qa-agent", "customer-faq")
    chunk = SimpleNamespace(
        file_path="architecture.md",
        start_line=1,
        end_line=4,
        chunk_type="markdown",
        name="architecture",
        language="markdown",
        content="Architecture overview",
        score=0.9,
    )
    retriever = SimpleNamespace(
        is_indexed=True,
        search=AsyncMock(return_value=[chunk]),
    )
    reranker = SimpleNamespace(rerank=MagicMock(return_value=[chunk]))
    monkeypatch.setattr(settings, "rerank_candidate_pool", 7)
    monkeypatch.setattr(
        "app.tools.rag_tool.get_retriever_for_tenant",
        AsyncMock(return_value=retriever),
    )
    monkeypatch.setattr(
        "app.rag.reranking.learned.get_learned_reranker",
        lambda: reranker,
    )

    await rag_search.ainvoke({"query": "architecture", "top_k": 3})

    retriever.search.assert_awaited_once_with("architecture", top_k=7)
    reranker.rerank.assert_called_once_with("architecture", [chunk], top_k=7)
