from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

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
