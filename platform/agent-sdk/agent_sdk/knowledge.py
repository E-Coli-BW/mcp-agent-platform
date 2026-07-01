"""Client for RAG knowledge base search."""

from dataclasses import dataclass


@dataclass
class SearchResult:
    """A single search result from the knowledge base."""

    content: str
    file_path: str
    name: str
    score: float


class KnowledgeBaseClient:
    """Thin wrapper around the existing retriever for plugin access."""

    def __init__(self, retriever=None):
        self._retriever = retriever

    async def search(self, query: str, top_k: int = 5) -> list[SearchResult]:
        """Search the knowledge base for relevant content."""
        if self._retriever is None:
            return []
        try:
            results = await self._retriever.search(query, top_k=top_k)
            return [
                SearchResult(
                    content=r.content,
                    file_path=r.file_path,
                    name=r.name,
                    score=r.score,
                )
                for r in results
            ]
        except Exception:
            return []
