"""Conversation store — Redis-backed sliding window for chat history.

HOW IT FITS IN THE SYSTEM:
  User message → [chat.py] → append to store → agent processes → append response
  Next request → [chat.py] → get_messages_for_llm() → provides conversation context

WHY REDIS (not in-memory)?
- Survives server restarts (FastAPI process crash → conversation preserved)
- Shared across multiple workers (if running gunicorn with multiple workers)
- Natural TTL support (sessions auto-expire after 30 minutes of inactivity)
- Graceful degradation: if Redis is down, the store becomes a no-op —
  the agent still works, just without conversation history

WHY SLIDING WINDOW (not full history)?
- LLM context window is limited (32K tokens for 7B model)
- Old messages are less relevant than recent ones
- 20 messages × ~200 tokens/msg = ~4K tokens — fits comfortably in context
- prompt modifier in graph.py provides additional compression on top of this

THREADING MODEL:
- All methods are async (redis.asyncio client)
- Uses the same asyncio event loop as FastAPI
- Redis operations are non-blocking (event loop sends command, awaits response)
- No connection pooling configured — creates one connection per store instance
  (acceptable for single-server setup; add pooling for production)

DATA MODEL IN REDIS:
- Key: "conv:{session_id}" → JSON string of message list
- Value: [{"role": "user", "content": "...", "timestamp": 1234567890}, ...]
- TTL: 1800 seconds (30 minutes) — auto-expires idle sessions
- Each write (append) resets the TTL → active sessions stay alive

GRACEFUL DEGRADATION:
- Every Redis operation is wrapped in try/except
- If Redis connection fails → return empty list / do nothing
- The agent continues to work, just without conversation history
- This is the "Circuit Breaker" pattern applied manually
"""

import json
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass, field, asdict

import redis.asyncio as redis

from app.config import settings

logger = logging.getLogger(__name__)


# ── Local LRU fallback when Redis is unavailable ──────────────
_LOCAL_CACHE_MAX_SESSIONS = 200


class _LocalLRUCache:
    """Bounded LRU cache for conversation history when Redis is down.

    NOT a replacement for Redis — only holds the most recent N sessions
    so the agent can still function (with reduced history) during outages.
    Evicts least-recently-used sessions when capacity is reached.
    """

    def __init__(self, max_sessions: int = _LOCAL_CACHE_MAX_SESSIONS):
        self._data: OrderedDict[str, list[dict]] = OrderedDict()
        self._max = max_sessions

    def get(self, key: str) -> list[dict]:
        if key in self._data:
            self._data.move_to_end(key)
            return self._data[key]
        return []

    def append(self, key: str, msg: dict, max_messages: int = 50):
        if key not in self._data:
            self._data[key] = []
        self._data.move_to_end(key)
        self._data[key].append(msg)
        # Sliding window
        if len(self._data[key]) > max_messages:
            self._data[key] = self._data[key][-max_messages:]
        # Evict oldest sessions
        while len(self._data) > self._max:
            self._data.popitem(last=False)

    def delete(self, key: str):
        self._data.pop(key, None)


_local_cache = _LocalLRUCache()


@dataclass
class Message:
    """A single message in the conversation history.
    
    Matches OpenAI's message format (role + content) with extras.
    The timestamp is used for ordering and TTL decisions.
    tool_name is set when the message is a tool result (for debugging).
    """
    role: str        # "user", "assistant", "tool"
    content: str     # The message text
    timestamp: float = field(default_factory=time.time)  # Unix epoch seconds
    tool_name: str | None = None  # Which tool generated this (if role="tool")


class ConversationStore:
    """Redis-backed conversation store with sliding window.
    
    Keeps last MAX_MESSAGES messages per session.
    Sessions expire after TTL (default 30 min).
    
    SINGLETON: One instance shared across all requests (get_conversation_store()).
    Thread-safe because redis.asyncio operations are atomic at the command level
    and we're on a single event loop.
    """

    MAX_MESSAGES = 20  # Keep last N messages (sliding window)

    def __init__(self):
        # Lazy-initialized Redis connection
        # None until first use — allows server to start even if Redis is down
        self._redis: redis.Redis | None = None

    async def _get_redis(self) -> redis.Redis:
        """Get or create Redis connection with 2-second connect timeout.
        
        WHY LAZY INIT? If Redis isn't running when the server starts,
        we don't want the server to crash. By initializing on first use,
        the server starts fine and Redis failures are handled gracefully.
        
        WHY 2-SECOND TIMEOUT? Default is 30 seconds. If Redis is down,
        we don't want each request to wait 30 seconds before falling back.
        2 seconds is enough for localhost Redis; adjust for remote Redis.
        """
        if self._redis is None:
            try:
                self._redis = redis.from_url(
                    settings.redis_url,
                    decode_responses=True,
                    socket_connect_timeout=2,
                    max_connections=20,       # connection pool size
                )
                # Verify connection is alive
                await self._redis.ping()
            except Exception as e:
                logger.debug("Redis unavailable: %s", e)
                self._redis = None
        return self._redis

    def _key(self, session_id: str) -> str:
        return f"conv:{session_id}"

    async def get_history(self, session_id: str) -> list[dict]:
        """Get conversation history for a session.
        
        Uses Redis LIST (LRANGE) — each message is a separate list element.
        This avoids the read-modify-write race of GET/SET with a JSON blob.
        Falls back to local LRU cache if Redis is unavailable.
        """
        r = await self._get_redis()
        if not r:
            return _local_cache.get(session_id)
        try:
            items = await r.lrange(self._key(session_id), 0, -1)
            return [json.loads(item) for item in items] if items else []
        except Exception:
            return _local_cache.get(session_id)

    async def append(self, session_id: str, message: Message):
        """Append a message atomically using RPUSH + LTRIM (sliding window).
        
        RPUSH is atomic — concurrent appends never lose messages.
        LTRIM enforces the sliding window in the same pipeline.
        Always writes to local LRU cache as well (dual-write for failover).
        """
        msg_dict = asdict(message)
        _local_cache.append(session_id, msg_dict, max_messages=self.MAX_MESSAGES)

        r = await self._get_redis()
        if not r:
            return

        try:
            key = self._key(session_id)
            msg_json = json.dumps(msg_dict, ensure_ascii=False)

            # Atomic: append + trim + set TTL in a pipeline
            pipe = r.pipeline()
            pipe.rpush(key, msg_json)
            pipe.ltrim(key, -self.MAX_MESSAGES, -1)  # keep last N
            pipe.expire(key, settings.session_ttl_seconds)
            await pipe.execute()
        except Exception as e:
            logger.debug("Redis append failed (graceful degradation): %s", e)

    async def get_messages_for_llm(self, session_id: str) -> list[dict]:
        """Get messages formatted for LLM (role + content only)."""
        history = await self.get_history(session_id)
        return [
            {"role": m["role"], "content": m["content"]}
            for m in history
            if m["role"] in ("user", "assistant")
        ]

    async def clear(self, session_id: str):
        """Clear a session's history."""
        r = await self._get_redis()
        if r:
            try:
                await r.delete(self._key(session_id))
            except Exception as e:
                logger.debug("Redis clear failed: %s", e)


# Singleton
_store = ConversationStore()


def get_conversation_store() -> ConversationStore:
    return _store
