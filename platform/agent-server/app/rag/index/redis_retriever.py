"""Redis-backed vector retriever — replaces InMemoryRetriever for production.

Uses Redis Stack with RediSearch module for vector similarity search.
Falls back to InMemoryRetriever if Redis is unavailable.

Requirements:
    - Redis Stack (with RediSearch module) or Redis 7+ with redis-stack image
    - pip install redis[hiredis]

Usage:
    export AGENT_RAG_BACKEND=redis   # or "memory" (default)
    # Redis URL is read from config.py (shared with conversation store)
    # Must be db 0 for RediSearch (Pitfall #28)
"""

import json
import re
import logging
from dataclasses import asdict

import numpy as np
import redis

from app.rag.chunking.code import CodeChunk
from app.rag.embedding.embedder import embed_text
from app.rag.index.retriever import ScoredChunk, VectorRetriever

from app.config import settings

logger = logging.getLogger(__name__)

# Use centralized Redis URL from config (shared with conversation store)
# Must be db 0 for RediSearch FT.CREATE (Pitfall #28)
VECTOR_DIM = 1024  # mxbai-embed-large dimension
INDEX_NAME = "idx:rag_chunks"
KEY_PREFIX = "rag:chunk:"


class RedisVectorRetriever(VectorRetriever):
    """Redis-backed hybrid retriever with vector + keyword search.
    
    Uses RediSearch for:
    - VECTOR similarity search (HNSW index on embeddings)
    - Full-text search (FT.SEARCH on content + name fields)
    - Combined scoring via RRF merge
    """

    def __init__(self, redis_url: str | None = None):
        self._redis_url = redis_url or settings.redis_url
        self._client: redis.Redis | None = None
        self._indexed = False

    @property
    def is_indexed(self) -> bool:
        return self._indexed

    def _get_redis(self) -> redis.Redis:
        if self._client is None:
            self._client = redis.from_url(self._redis_url, decode_responses=False)
        return self._client

    def _ensure_index(self):
        """Create RediSearch index if it doesn't exist."""
        r = self._get_redis()
        try:
            r.execute_command("FT.INFO", INDEX_NAME)
            logger.info("RediSearch index '%s' already exists", INDEX_NAME)
        except redis.ResponseError:
            # Create the index
            r.execute_command(
                "FT.CREATE", INDEX_NAME, "ON", "HASH", "PREFIX", "1", KEY_PREFIX,
                "SCHEMA",
                "content", "TEXT", "WEIGHT", "1.0",
                "file_path", "TEXT", "WEIGHT", "0.5",
                "name", "TEXT", "WEIGHT", "2.0",
                "chunk_type", "TAG",
                "language", "TAG",
                "start_line", "NUMERIC",
                "end_line", "NUMERIC",
                "embedding", "VECTOR", "HNSW", "6",
                    "TYPE", "FLOAT32",
                    "DIM", str(VECTOR_DIM),
                    "DISTANCE_METRIC", "COSINE",
            )
            logger.info("Created RediSearch index '%s'", INDEX_NAME)

    def load_index(self, chunks: list[CodeChunk], embeddings: list[list[float]]):
        """Store chunks and embeddings in Redis."""
        r = self._get_redis()
        self._ensure_index()

        pipe = r.pipeline(transaction=False)
        for i, (chunk, emb) in enumerate(zip(chunks, embeddings)):
            key = f"{KEY_PREFIX}{i}"
            emb_bytes = np.array(emb, dtype=np.float32).tobytes()
            pipe.hset(key, mapping={
                "content": chunk.content,
                "file_path": chunk.file_path,
                "language": chunk.language,
                "name": chunk.name,
                "chunk_type": chunk.chunk_type,
                "start_line": str(chunk.start_line),
                "end_line": str(chunk.end_line),
                "embedding": emb_bytes,
            })
        pipe.execute()
        self._indexed = True
        logger.info("Indexed %d chunks in Redis", len(chunks))

    async def search(self, query: str, top_k: int = 10) -> list[ScoredChunk]:
        """Hybrid search: vector KNN + full-text, merged with RRF."""
        r = self._get_redis()

        # Get query embedding
        query_emb = await embed_text(query)
        query_vec = np.array(query_emb, dtype=np.float32)
        query_vec = query_vec / (np.linalg.norm(query_vec) or 1)
        query_bytes = query_vec.tobytes()

        # Vector search (KNN)
        vector_results = []
        try:
            results = r.execute_command(
                "FT.SEARCH", INDEX_NAME,
                f"*=>[KNN {top_k * 2} @embedding $query_vec AS score]",
                "PARAMS", "2", "query_vec", query_bytes,
                "SORTBY", "score",
                "LIMIT", "0", str(top_k * 2),
                "RETURN", "7", "content", "file_path", "language", "name",
                "chunk_type", "start_line", "end_line",
                "DIALECT", "2",
            )
            vector_results = self._parse_search_results(results)
        except Exception as e:
            logger.warning("Vector search failed: %s", e)

        # Keyword search
        keyword_results = []
        try:
            # Escape special chars for RediSearch query
            safe_query = re.sub(r'[^\w\s]', ' ', query)
            terms = ' '.join(f"{t}" for t in safe_query.split() if len(t) > 1)
            if terms:
                results = r.execute_command(
                    "FT.SEARCH", INDEX_NAME, terms,
                    "LIMIT", "0", str(top_k * 2),
                    "RETURN", "7", "content", "file_path", "language", "name",
                    "chunk_type", "start_line", "end_line",
                )
                keyword_results = self._parse_search_results(results)
        except Exception as e:
            logger.warning("Keyword search failed: %s", e)

        # RRF merge
        return self.rrf(vector_results, keyword_results)[:top_k]

    def _parse_search_results(self, results) -> list[ScoredChunk]:
        """Parse FT.SEARCH results into ScoredChunks."""
        if not results or results[0] == 0:
            return []
        chunks = []
        # Results format: [count, key1, [field, value, ...], key2, ...]
        i = 1
        while i < len(results):
            key = results[i]
            fields = results[i + 1] if i + 1 < len(results) else []
            i += 2
            field_dict = {}
            for j in range(0, len(fields), 2):
                k = fields[j].decode() if isinstance(fields[j], bytes) else fields[j]
                v = fields[j + 1].decode() if isinstance(fields[j + 1], bytes) else fields[j + 1]
                field_dict[k] = v
            chunks.append(ScoredChunk(
                content=field_dict.get("content", ""),
                file_path=field_dict.get("file_path", ""),
                language=field_dict.get("language", ""),
                name=field_dict.get("name", ""),
                chunk_type=field_dict.get("chunk_type", ""),
                start_line=int(field_dict.get("start_line", 0)),
                end_line=int(field_dict.get("end_line", 0)),
            ))
        return chunks

    def load_from_disk(self, path: str) -> bool:
        """Load from disk index into Redis (migration path)."""
        from pathlib import Path
        p = Path(path)
        if not (p / "embeddings.npy").exists():
            return False
        embeddings = np.load(p / "embeddings.npy")
        chunks_data = json.loads((p / "chunks.json").read_text())
        chunks = [CodeChunk(**d, last_modified=None) for d in chunks_data]
        self.load_index(chunks, embeddings.tolist())
        return True
