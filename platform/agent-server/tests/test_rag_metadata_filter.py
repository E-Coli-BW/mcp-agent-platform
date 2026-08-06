import numpy as np
import pytest

pytest.importorskip("tree_sitter_python")

from app.rag.chunking.code import CodeChunk
from app.rag.index.retriever import InMemoryRetriever, _matches_metadata


def test_metadata_filter_requires_exact_tenant_acl_and_version():
    assert _matches_metadata({"tenant_id": "t1", "version": "v2"}, {"tenant_id": "t1", "version": "v2"})
    assert not _matches_metadata({"tenant_id": "t2", "version": "v2"}, {"tenant_id": "t1"})
    assert not _matches_metadata({}, {"tenant_id": "t1"})


@pytest.mark.asyncio
async def test_retriever_filters_before_returning_results(monkeypatch):
    async def fake_embed(text):
        return [1.0, 0.0]

    monkeypatch.setattr("app.rag.index.retriever.embed_text", fake_embed)
    retriever = InMemoryRetriever()
    chunks = [
        CodeChunk("allowed", "a.md", "markdown", "module", "a", 1, 1, metadata={"tenant_id": "t1"}),
        CodeChunk("blocked", "b.md", "markdown", "module", "b", 1, 1, metadata={"tenant_id": "t2"}),
    ]
    retriever.load_index(chunks, [[1.0, 0.0], [1.0, 0.0]])
    results = await retriever.search("allowed", top_k=5, metadata_filter={"tenant_id": "t1"})
    assert [result.file_path for result in results] == ["a.md"]
