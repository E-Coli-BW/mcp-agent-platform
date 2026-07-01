"""Vector retriever SPI — pluggable backends for RAG vector search.

Implementations:
  - InMemoryRetriever  (numpy, default for dev)
  - RedisVectorRetriever (RediSearch HNSW, for production)
  - Future: MilvusRetriever, PgVectorRetriever, ChromaRetriever

Select via: AGENT_RAG_BACKEND=memory|redis|milvus  (default: memory)
"""

import asyncio
import json
import logging
import os
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from app.auth.middleware import tenant_context
from app.rag.chunking.code import CodeChunk
from app.rag.embedding.embedder import embed_text

logger = logging.getLogger(__name__)

DEFAULT_COLLECTION = "default"


@dataclass
class ScoredChunk:
    """A code chunk with a relevance score from search."""

    content: str
    file_path: str
    language: str
    name: str
    chunk_type: str
    start_line: int
    end_line: int
    score: float = 0.0


class VectorRetriever(ABC):
    """Abstract base for vector search backends.

    All implementations must support:
    - load_index: bulk insert chunks + embeddings
    - search: hybrid vector + keyword search with RRF merge
    - is_indexed: whether the index has data

    Adding a new backend (e.g., Milvus):
    1. Create milvus_retriever.py
    2. Implement VectorRetriever interface
    3. Add "milvus" case to get_retriever() factory below
    """

    @property
    @abstractmethod
    def is_indexed(self) -> bool:
        """Whether the index has been loaded with data."""
        ...

    @abstractmethod
    def load_index(self, chunks: list[CodeChunk], embeddings: list[list[float]]):
        """Bulk load chunks and their embeddings into the index."""
        ...

    @abstractmethod
    async def search(self, query: str, top_k: int = 10) -> list[ScoredChunk]:
        """Search for relevant chunks. Returns scored results sorted by relevance."""
        ...

    def save_index(self, path: str):
        """Save index to disk (optional — not all backends need this)."""
        raise NotImplementedError("This backend does not support disk persistence")

    def load_from_disk(self, path: str) -> bool:
        """Load index from disk (optional)."""
        raise NotImplementedError("This backend does not support disk loading")

    @staticmethod
    def rrf(*result_lists: list[ScoredChunk], k: int = 60) -> list[ScoredChunk]:
        """Reciprocal Rank Fusion — merges multiple ranked lists."""
        scores: dict[str, float] = {}
        chunks: dict[str, ScoredChunk] = {}
        for results in result_lists:
            for rank, chunk in enumerate(results):
                key = f"{chunk.file_path}:{chunk.start_line}"
                scores[key] = scores.get(key, 0) + 1.0 / (k + rank + 1)
                chunks[key] = chunk
        for key in chunks:
            chunks[key].score = scores[key]
        sorted_keys = sorted(scores, key=scores.get, reverse=True)
        return [chunks[k] for k in sorted_keys]


class InMemoryRetriever(VectorRetriever):
    """In-memory hybrid retriever with vector search + keyword search + RRF.

    Stores embeddings as numpy arrays in memory.
    Good for dev/testing; data lost on restart.
    """

    def __init__(self):
        self.chunks: list[CodeChunk] = []
        self.embeddings: np.ndarray | None = None  # shape: (n_chunks, embed_dim)
        self._indexed = False

    @property
    def is_indexed(self) -> bool:
        return self._indexed and len(self.chunks) > 0

    def load_index(self, chunks: list[CodeChunk], embeddings: list[list[float]]):
        """Load pre-computed chunks and embeddings."""
        self.chunks = chunks
        self.embeddings = np.array(embeddings, dtype=np.float32)
        # Normalize for cosine similarity
        norms = np.linalg.norm(self.embeddings, axis=1, keepdims=True)
        norms[norms == 0] = 1
        self.embeddings = self.embeddings / norms
        self._indexed = True

    def save_index(self, path: str):
        """Save index to disk for fast reload."""
        p = Path(path)
        p.mkdir(parents=True, exist_ok=True)
        np.save(p / "embeddings.npy", self.embeddings)
        chunks_data = [
            {
                "content": c.content,
                "file_path": c.file_path,
                "language": c.language,
                "chunk_type": c.chunk_type,
                "name": c.name,
                "start_line": c.start_line,
                "end_line": c.end_line,
            }
            for c in self.chunks
        ]
        (p / "chunks.json").write_text(json.dumps(chunks_data, ensure_ascii=False))

    def load_from_disk(self, path: str) -> bool:
        """Load saved index from disk."""
        p = Path(path)
        if not (p / "embeddings.npy").exists():
            return False
        self.embeddings = np.load(p / "embeddings.npy")
        chunks_data = json.loads((p / "chunks.json").read_text())
        self.chunks = [CodeChunk(**d, last_modified=None) for d in chunks_data]
        self._indexed = True
        return True

    async def search(self, query: str, top_k: int = 10) -> list[ScoredChunk]:
        """Hybrid search: vector + keyword, merged with RRF."""
        if not self.is_indexed:
            return []

        # Vector search
        query_emb = await embed_text(query)
        query_vec = np.array(query_emb, dtype=np.float32)
        query_vec = query_vec / (np.linalg.norm(query_vec) or 1)
        vector_scores = self.embeddings @ query_vec  # cosine similarity
        vector_top = np.argsort(vector_scores)[::-1][: top_k * 2]

        vector_results = [
            ScoredChunk(
                content=self.chunks[i].content,
                file_path=self.chunks[i].file_path,
                language=self.chunks[i].language,
                name=self.chunks[i].name,
                chunk_type=self.chunks[i].chunk_type,
                start_line=self.chunks[i].start_line,
                end_line=self.chunks[i].end_line,
                score=float(vector_scores[i]),
            )
            for i in vector_top
        ]

        # Keyword search (simple BM25-like TF matching)
        query_tokens = set(re.split(r"\W+", query.lower()))
        keyword_scores = []
        for i, chunk in enumerate(self.chunks):
            text = (chunk.content + " " + chunk.name).lower()
            tokens = re.split(r"\W+", text)
            if not tokens:
                keyword_scores.append(0)
                continue
            matches = sum(1 for t in tokens if t in query_tokens)
            keyword_scores.append(matches / (len(tokens) ** 0.5))

        keyword_top = np.argsort(keyword_scores)[::-1][: top_k * 2]
        keyword_results = [
            ScoredChunk(
                content=self.chunks[i].content,
                file_path=self.chunks[i].file_path,
                language=self.chunks[i].language,
                name=self.chunks[i].name,
                chunk_type=self.chunks[i].chunk_type,
                start_line=self.chunks[i].start_line,
                end_line=self.chunks[i].end_line,
                score=float(keyword_scores[i]),
            )
            for i in keyword_top
            if keyword_scores[i] > 0
        ]

        # RRF merge
        merged = self.rrf(vector_results, keyword_results)
        return merged[:top_k]

    @staticmethod
    def _rrf(*result_lists: list[ScoredChunk], k: int = 60) -> list[ScoredChunk]:
        """Reciprocal Rank Fusion — delegated to base class."""
        return VectorRetriever.rrf(*result_lists, k=k)


# ── Factory ───────────────────────────────────────────────────

_retriever_cache: dict[str, VectorRetriever] = {}
_retriever_lock = asyncio.Lock()
# TODO: Add LRU eviction policy for the retriever cache.


def _build_retriever(backend: str) -> VectorRetriever:
    """Build a new retriever instance for the configured backend."""
    if backend == "redis":
        try:
            from app.rag.index.redis_retriever import RedisVectorRetriever

            retriever = RedisVectorRetriever()
            retriever._get_redis().ping()
            logger.info("✅ RAG backend: Redis (RediSearch HNSW)")
            return retriever
        except Exception as e:
            logger.warning("Redis unavailable, falling back to in-memory: %s", e)

    elif backend == "milvus":
        try:
            from app.rag.index.milvus_retriever import MilvusRetriever

            retriever = MilvusRetriever()
            logger.info("✅ RAG backend: Milvus")
            return retriever
        except ImportError:
            logger.warning("pymilvus not installed, falling back to in-memory")
        except Exception as e:
            logger.warning("Milvus unavailable: %s", e)

    logger.info("✅ RAG backend: InMemory (numpy)")
    return InMemoryRetriever()


async def get_retriever_for_tenant(
    tenant_id: str,
    collection: str | None = None,
) -> VectorRetriever:
    """Return an isolated retriever instance for a tenant/backend/collection tuple."""
    coll = collection or DEFAULT_COLLECTION
    backend = os.environ.get("AGENT_RAG_BACKEND", "memory").lower()
    cache_key = f"{tenant_id}:{backend}:{coll}"

    retriever = _retriever_cache.get(cache_key)
    if retriever is not None:
        return retriever

    async with _retriever_lock:
        retriever = _retriever_cache.get(cache_key)
        if retriever is not None:
            return retriever

        retriever = _build_retriever(backend)
        from app.rag.index.indexer import get_index_dir_for_tenant

        tenant_index = get_index_dir_for_tenant(tenant_id, coll)
        try:
            retriever.load_from_disk(str(tenant_index))
        except NotImplementedError:
            logger.debug("RAG backend %s does not support disk loading", backend)
        except Exception as e:
            logger.warning("Failed to load RAG index for tenant %s: %s", tenant_id, e)

        _retriever_cache[cache_key] = retriever
        return retriever


def get_retriever() -> VectorRetriever:
    """Backward-compatible sync factory that returns a cached tenant retriever."""
    tenant_id = tenant_context.get("default")
    backend = os.environ.get("AGENT_RAG_BACKEND", "memory").lower()
    cache_key = f"{tenant_id}:{backend}:{DEFAULT_COLLECTION}"

    retriever = _retriever_cache.get(cache_key)
    if retriever is not None:
        return retriever

    retriever = _build_retriever(backend)
    _retriever_cache[cache_key] = retriever
    return retriever


def clear_tenant_cache(tenant_id: str) -> None:
    """Evict all cache entries for a tenant. Used by tests."""
    keys_to_remove = [key for key in _retriever_cache if key.startswith(f"{tenant_id}:")]
    for key in keys_to_remove:
        del _retriever_cache[key]
