from pathlib import Path

import pytest

pytest.importorskip("tree_sitter_python")

from app.rag.chunking.registry import chunk_file


def test_markdown_chunks_have_stable_source_hierarchy(tmp_path):
    path = tmp_path / "policy.md"
    path.write_text("# Returns\n\nReturn within 30 days.\n\n## Exceptions\n\nDamaged items.")
    chunks = chunk_file(path)
    assert len(chunks) == 2
    for chunk in chunks:
        assert chunk.metadata["document_id"]
        assert chunk.metadata["parent_id"].startswith(chunk.metadata["document_id"])
        assert chunk.metadata["document_parent_id"] == f"{chunk.metadata['document_id']}:document"
        assert chunk.metadata["source_type"] == "md"
        assert chunk.metadata["acl"] == ["*"]


def test_plain_text_chunks_have_version_and_chunk_id(tmp_path):
    path = tmp_path / "faq.txt"
    path.write_text("question\nanswer\n")
    chunk = chunk_file(path)[0]
    assert chunk.metadata["version"].isdigit()
    assert chunk.metadata["chunk_id"].startswith(chunk.metadata["document_id"])
