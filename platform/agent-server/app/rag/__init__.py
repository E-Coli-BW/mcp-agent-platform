"""RAG (Retrieval-Augmented Generation) subsystem.

Organized into six concerns:

    chunking/    — split source files into semantic units (code, markdown, HTML, ...)
    embedding/   — text → dense vectors (Ollama)
    index/       — vector retrievers (memory, Redis) + bulk indexer
    reranking/   — strategy dispatcher + LLM / cross-encoder / heuristic / learned
    compression/ — AST-aware compression of tool output
    eval/        — benchmark dataset + reranker/compression eval scripts

This top-level package re-exports the most common public symbols for backward
compatibility with code that imports from `app.rag.<name>` directly. New code
should prefer the subpackage paths (e.g. `from app.rag.reranking import rerank`)
for clarity.

Note: the subpackage is called ``reranking`` (gerund) rather than ``rerank``
to avoid shadowing the re-exported ``rerank()`` function.
"""

# Chunking
from app.rag.chunking.code import (
    Chunk,
    CodeChunk,
    LANGUAGES,
    LANGUAGE_NAMES,
    chunk_file,
)
from app.rag.chunking.registry import chunk_directory

# Embedding
from app.rag.embedding.embedder import (
    MAX_EMBED_CHARS,
    embed_text,
    embed_texts,
)

# Index
from app.rag.index.retriever import (
    InMemoryRetriever,
    ScoredChunk,
    VectorRetriever,
    get_retriever,
)
from app.rag.index.indexer import (
    BASE_INDEX_DIR,
    DEFAULT_COLLECTION,
    get_index_dir_for_workspace,
)

# Rerank
from app.rag.reranking.dispatcher import (
    arerank,
    heuristic_rerank,
    rerank,
)
from app.rag.reranking.cross_encoder import cross_encoder_rerank
from app.rag.reranking.llm import llm_rerank
from app.rag.reranking.learned import get_learned_reranker

# Compression
from app.rag.compression.ast import compress_code_output

__all__ = [
    # chunking
    "Chunk",
    "CodeChunk",
    "LANGUAGES",
    "LANGUAGE_NAMES",
    "chunk_file",
    "chunk_directory",
    # embedding
    "embed_text",
    "embed_texts",
    "MAX_EMBED_CHARS",
    # index
    "ScoredChunk",
    "VectorRetriever",
    "InMemoryRetriever",
    "get_retriever",
    "BASE_INDEX_DIR",
    "DEFAULT_COLLECTION",
    "get_index_dir_for_workspace",
    # rerank
    "rerank",
    "arerank",
    "heuristic_rerank",
    "cross_encoder_rerank",
    "llm_rerank",
    "get_learned_reranker",
    # compression
    "compress_code_output",
]
