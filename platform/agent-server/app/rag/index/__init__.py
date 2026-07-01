"""Index — vector retrievers and bulk indexer.

Modules:
    retriever       — ScoredChunk, VectorRetriever SPI, InMemoryRetriever, get_retriever()
    redis_retriever — RedisVectorRetriever (production backend)
    indexer         — CLI + library for bulk-indexing a workspace

Public entry points:
    from app.rag.index import ScoredChunk, get_retriever, BASE_INDEX_DIR
"""

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

__all__ = [
    "ScoredChunk",
    "VectorRetriever",
    "InMemoryRetriever",
    "get_retriever",
    "BASE_INDEX_DIR",
    "DEFAULT_COLLECTION",
    "get_index_dir_for_workspace",
]
