"""HTTP client for the memory-server REST API."""

import json
from collections.abc import Awaitable, Callable

import httpx


class MemoryUnavailableError(RuntimeError):
    """Raised when plugin memory cannot reach or decode its backend."""


class MemoryClient:
    """Client for interacting with the memory-server."""

    def __init__(
        self,
        base_url: str | None = None,
        auth_token: str | None = None,
        *,
        tool_caller: Callable[[str, dict], Awaitable[str]] | None = None,
    ):
        self._base_url = (base_url or "").rstrip("/")
        self._auth_token = auth_token or ""
        self._tool_caller = tool_caller

    async def _call(self, tool_name: str, payload: dict) -> str:
        try:
            if self._tool_caller is not None:
                result = await self._tool_caller(tool_name, payload)
            else:
                if not self._base_url or not self._auth_token:
                    raise MemoryUnavailableError(
                        "memory backend or authentication is not configured"
                    )
                async with httpx.AsyncClient() as client:
                    response = await client.post(
                        f"{self._base_url}/api/tools/{tool_name}",
                        json=payload,
                        headers={"Authorization": f"Bearer {self._auth_token}"},
                        timeout=10.0,
                    )
                    response.raise_for_status()
                    result = str(response.json().get("result", ""))
        except MemoryUnavailableError:
            raise
        except Exception as exc:
            raise MemoryUnavailableError(f"memory backend unavailable: {exc}") from exc

        # The Java bridge uses a user-facing ❌ prefix for a missing memory key.
        # That is an expected lookup outcome, not a backend availability error.
        if (
            tool_name == "memory_get"
            and result.startswith("❌ No memory found with key ")
        ):
            return result.removeprefix("❌ ")
        if not result or result.startswith(("❌", "Error [", "Cannot connect", "HTTP ")):
            raise MemoryUnavailableError(result or "memory backend returned an empty result")
        return result

    @staticmethod
    def _json_value(result: str, expected_type: type):
        starts = [index for marker in ("[", "{") if (index := result.find(marker)) >= 0]
        if not starts:
            raise MemoryUnavailableError("memory backend returned no structured result")
        try:
            value, _ = json.JSONDecoder().raw_decode(result[min(starts):])
        except json.JSONDecodeError as exc:
            raise MemoryUnavailableError("memory backend returned invalid JSON") from exc
        if not isinstance(value, expected_type):
            raise MemoryUnavailableError("memory backend returned an unexpected result shape")
        return value

    async def search(
        self, query: str, namespace: str | None = None, top_k: int = 10
    ) -> list[dict]:
        """Search memories for relevant context."""
        payload: dict = {"query": query, "limit": top_k}
        if namespace:
            payload["namespace"] = namespace
        result = await self._call("memory_search", payload)
        if "No memories found" in result:
            return []
        return self._json_value(result, list)

    async def set(self, key: str, content: str, tags: list[str] | None = None) -> str:
        """Save a memory entry."""
        payload: dict = {"key": key, "content": content}
        if tags:
            payload["tags"] = tags
        return await self._call("memory_set", payload)

    async def get(self, key: str) -> str | None:
        """Retrieve a memory entry by key."""
        result = await self._call("memory_get", {"key": key})
        if result.startswith("No memory found"):
            return None
        data = self._json_value(result, dict)
        content = data.get("content")
        return str(content) if content is not None else None
