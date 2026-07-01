"""Chunking — splits source files into semantically meaningful units.

Modules:
    code        — tree-sitter-based code chunker (Chunk, CodeChunk, chunk_file)
    registry    — file-extension dispatcher → right chunker (chunk_directory)
    fixed_size  — line-window fallback for plain text
    html        — heading-based HTML splitter
    markdown    — heading-based markdown splitter
    openapi     — OpenAPI-aware spec + YAML/JSON fallback
    pdf         — PDF page-based chunker

Public entry points:
    from app.rag.chunking import Chunk, chunk_file, chunk_directory
"""

from app.rag.chunking.code import (
    Chunk,
    CodeChunk,
    LANGUAGES,
    LANGUAGE_NAMES,
    chunk_file,
    chunk_directory as chunk_directory_code,
)
from app.rag.chunking.registry import chunk_directory, chunk_file as chunk_file_routed

__all__ = [
    "Chunk",
    "CodeChunk",
    "LANGUAGES",
    "LANGUAGE_NAMES",
    "chunk_file",
    "chunk_file_routed",
    "chunk_directory",
    "chunk_directory_code",
]
