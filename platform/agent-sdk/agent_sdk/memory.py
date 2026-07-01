"""HTTP client for the memory-server REST API."""

import httpx


class MemoryClient:
    """Client for interacting with the memory-server."""

    def __init__(self, base_url: str, auth_token: str):
        self._base_url = base_url.rstrip("/")
        self._auth_token = auth_token

    async def search(
        self, query: str, namespace: str | None = None, top_k: int = 10
    ) -> list[dict]:
        """Search memories for relevant context."""
        payload: dict = {"query": query, "top_k": top_k}
        if namespace:
            payload["namespace"] = namespace
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"{self._base_url}/api/tools/memory_search",
                    json=payload,
                    headers={"Authorization": f"Bearer {self._auth_token}"},
                    timeout=10.0,
                )
                resp.raise_for_status()
                return resp.json().get("results", [])
        except Exception:
            return []

    async def set(self, key: str, content: str, tags: list[str] | None = None) -> str:
        """Save a memory entry."""
        payload: dict = {"key": key, "content": content}
        if tags:
            payload["tags"] = tags
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"{self._base_url}/api/tools/memory_set",
                    json=payload,
                    headers={"Authorization": f"Bearer {self._auth_token}"},
                    timeout=10.0,
                )
                resp.raise_for_status()
                return resp.json().get("result", "ok")
        except Exception:
            return ""

    async def get(self, key: str) -> str | None:
        """Retrieve a memory entry by key."""
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"{self._base_url}/api/tools/memory_get",
                    json={"key": key},
                    headers={"Authorization": f"Bearer {self._auth_token}"},
                    timeout=10.0,
                )
                resp.raise_for_status()
                data = resp.json()
                return data.get("content")
        except Exception:
            return None
