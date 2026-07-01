"""Session LANE — serialize concurrent requests for the same session.

Prevents interleaved tool calls when multiple requests hit the same session.
Uses Redis SET NX EX for distributed locking with automatic expiry.
Degrades gracefully when Redis is unavailable (no locking).
"""

import asyncio
import logging

logger = logging.getLogger(__name__)

# Shared Redis connection — lazy-initialized
_redis = None


async def _get_redis():
    global _redis
    if _redis is None:
        try:
            import redis.asyncio as aioredis
            from app.config import settings
            _redis = aioredis.from_url(settings.redis_url)
        except Exception as e:
            logger.debug("Redis connection failed: %s", e)
            return None
    return _redis


async def acquire_session_lock(session_id: str, timeout: int = 120) -> bool:
    """Try to acquire exclusive lock for a session.

    Returns True if acquired, False if already locked.
    Gracefully returns True if Redis is unavailable (no-op locking).
    """
    try:
        r = await _get_redis()
        if r is None:
            return True
        key = f"session_lane:{session_id}"
        result = await r.set(key, "1", nx=True, ex=timeout)
        return bool(result)
    except Exception as e:
        logger.debug("Session lock unavailable (Redis): %s", e)
        return True


async def release_session_lock(session_id: str) -> None:
    """Release session lock. No-op if Redis is unavailable."""
    try:
        r = await _get_redis()
        if r is None:
            return
        key = f"session_lane:{session_id}"
        await r.delete(key)
    except Exception as e:
        logger.debug("Session lock release failed (Redis): %s", e)


async def wait_for_session_lock(
    session_id: str, poll_interval: float = 0.5, max_wait: float = 30
) -> bool:
    """Wait until session lock is available, then acquire it.

    Returns True if lock acquired, False if timed out.
    Gracefully returns True if Redis is unavailable.
    """
    elapsed = 0.0
    while elapsed < max_wait:
        if await acquire_session_lock(session_id):
            return True
        await asyncio.sleep(poll_interval)
        elapsed += poll_interval
    return False
