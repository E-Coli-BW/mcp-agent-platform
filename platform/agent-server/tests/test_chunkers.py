"""Tests for pluggable RAG chunkers."""

import builtins

from app.rag.chunking.registry import chunk_file as registry_chunk_file
from app.rag.chunking.fixed_size import chunk_fixed_size
from app.rag.chunking.html import chunk_html
from app.rag.chunking.markdown import chunk_markdown
from app.rag.chunking.openapi import chunk_openapi_or_fallback
from app.rag.chunking.pdf import chunk_pdf


def test_markdown_chunker_splits_by_headings(tmp_path):
    file_path = tmp_path / "guide.md"
    file_path.write_text("# Intro\nOne\n## Details\nTwo\n### Deep Dive\nThree\n")

    chunks = chunk_markdown(file_path)

    assert [chunk.name for chunk in chunks] == ["Intro", "Details", "Deep Dive"]
    assert [chunk.chunk_type for chunk in chunks] == ["section", "section", "section"]


def test_markdown_chunker_no_headings_returns_whole_file(tmp_path):
    file_path = tmp_path / "notes.md"
    file_path.write_text("plain text only\nsecond line\n")

    chunks = chunk_markdown(file_path)

    assert len(chunks) == 1
    assert chunks[0].chunk_type == "document"
    assert chunks[0].content == "plain text only\nsecond line\n"


def test_openapi_chunker_extracts_endpoints(tmp_path):
    file_path = tmp_path / "openapi.yaml"
    file_path.write_text(
        """
openapi: 3.0.0
paths:
  /users:
    get:
      summary: List users
      description: Returns all users
      responses:
        '200':
          description: ok
  /users/{id}:
    post:
      summary: Create user
      parameters:
        - name: id
          in: path
          required: true
          schema:
            type: string
      responses:
        '201':
          description: created
""".strip()
    )

    chunks = chunk_openapi_or_fallback(file_path)
    endpoint_names = {chunk.name for chunk in chunks if chunk.chunk_type == "api_endpoint"}

    assert endpoint_names == {"GET /users", "POST /users/{id}"}


def test_openapi_chunker_extracts_schemas(tmp_path):
    file_path = tmp_path / "schemas.yaml"
    file_path.write_text(
        """
openapi: 3.0.0
components:
  schemas:
    User:
      type: object
      required: [id]
      properties:
        id:
          type: string
          description: User identifier
        name:
          type: string
          minLength: 1
""".strip()
    )

    chunks = chunk_openapi_or_fallback(file_path)
    schema_chunks = [chunk for chunk in chunks if chunk.chunk_type == "api_schema"]

    assert len(schema_chunks) == 1
    assert schema_chunks[0].name == "Schema: User"
    assert "- id: string (required)" in schema_chunks[0].content
    assert "- name: string (minLength=1)" in schema_chunks[0].content


def test_openapi_chunker_non_openapi_falls_back(tmp_path):
    file_path = tmp_path / "config.yaml"
    file_path.write_text("service:\n  name: demo\n")

    chunks = chunk_openapi_or_fallback(file_path)

    assert len(chunks) == 1
    assert chunks[0].chunk_type == "text_block"
    assert chunks[0].language == "yaml"


def test_fixed_size_chunker_splits_by_lines(tmp_path):
    file_path = tmp_path / "large.txt"
    file_path.write_text("\n".join(f"line {i}" for i in range(1, 101)))

    chunks = chunk_fixed_size(file_path, max_lines=50, overlap=5)

    assert len(chunks) == 3
    assert chunks[0].start_line == 1 and chunks[0].end_line == 50
    assert chunks[1].start_line == 46 and chunks[1].end_line == 95
    assert chunks[2].start_line == 91 and chunks[2].end_line == 100


def test_pdf_chunker_graceful_without_pypdf2(tmp_path, monkeypatch):
    file_path = tmp_path / "doc.pdf"
    file_path.write_bytes(b"%PDF-1.4\n")

    real_import = builtins.__import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "PyPDF2":
            raise ImportError("missing PyPDF2")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    chunks = chunk_pdf(file_path)

    assert len(chunks) == 1
    assert "PyPDF2" in chunks[0].content


def test_chunker_registry_dispatches_by_extension(tmp_path):
    markdown_path = tmp_path / "doc.md"
    markdown_path.write_text("# Title\nBody\n")
    python_path = tmp_path / "code.py"
    python_path.write_text("def hello():\n    return 'world'\n")
    text_path = tmp_path / "notes.txt"
    text_path.write_text("alpha\nbeta\n")

    markdown_chunks = registry_chunk_file(markdown_path)
    python_chunks = registry_chunk_file(python_path)
    text_chunks = registry_chunk_file(text_path)

    assert markdown_chunks[0].language == "markdown"
    assert python_chunks[0].language == "python"
    assert text_chunks[0].chunk_type == "text_block"


def test_html_chunker_strips_tags(tmp_path):
    file_path = tmp_path / "page.html"
    file_path.write_text(
        "<h1>Overview</h1><p><strong>Hello</strong> world</p><h2>Next</h2><div>Done</div>"
    )

    chunks = chunk_html(file_path)

    assert [chunk.name for chunk in chunks] == ["Overview", "Next"]
    assert all("<" not in chunk.content for chunk in chunks)
    assert "Hello world" in chunks[0].content
