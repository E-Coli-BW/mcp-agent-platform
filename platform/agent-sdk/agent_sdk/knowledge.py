"""Client for RAG knowledge base search."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any


@dataclass
class SearchResult:
    """A single search result from the knowledge base."""

    content: str
    file_path: str
    name: str
    score: float


class KnowledgeUnavailableError(RuntimeError):
    """Raised when a plugin knowledge capability has no usable retriever."""


class KnowledgeBaseClient:
    """Thin wrapper around the existing retriever for plugin access."""

    def __init__(
        self,
        retriever: Any | None = None,
        *,
        retriever_factory: Callable[[], Awaitable[Any]] | None = None,
    ):
        self._retriever = retriever
        self._retriever_factory = retriever_factory

    async def _resolve_retriever(self) -> Any:
        if self._retriever is not None:
            return self._retriever
        if self._retriever_factory is None:
            raise KnowledgeUnavailableError("knowledge retriever is not configured")
        try:
            retriever = await self._retriever_factory()
        except Exception as exc:
            raise KnowledgeUnavailableError(
                f"knowledge retriever is unavailable: {exc}"
            ) from exc
        if retriever is None:
            raise KnowledgeUnavailableError("knowledge retriever is unavailable")
        return retriever

    async def search(self, query: str, top_k: int = 5) -> list[SearchResult]:
        """Search the knowledge base for relevant content."""
        retriever = await self._resolve_retriever()
        try:
            results = await retriever.search(query, top_k=top_k)
            return [
                SearchResult(
                    content=r.content,
                    file_path=r.file_path,
                    name=r.name,
                    score=r.score,
                )
                for r in results
            ]
        except Exception as exc:
            raise KnowledgeUnavailableError(
                f"knowledge search failed: {exc}"
            ) from exc
