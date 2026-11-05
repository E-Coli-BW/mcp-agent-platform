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
SESSION_LOCK_TTL_SECONDS = 900
SESSION_LOCK_RENEW_INTERVAL_SECONDS = SESSION_LOCK_TTL_SECONDS // 3

_RELEASE_IF_OWNER_SCRIPT = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
end
return 0
"""

_RENEW_IF_OWNER_SCRIPT = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("pexpire", KEYS[1], ARGV[2])
end
return 0
"""


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


async def acquire_session_lock(
    session_id: str, owner_token: str, timeout: int = 900
) -> bool:
    """Try to acquire exclusive lock for a session.

    The unique owner token is stored as the lock value for safe release.
    Returns True if acquired, False if already locked.
    Gracefully returns True if Redis is unavailable (no-op locking).
    """
    try:
        r = await _get_redis()
        if r is None:
            return True
        key = f"session_lane:{session_id}"
        result = await r.set(key, owner_token, nx=True, ex=timeout)
        return bool(result)
    except Exception as e:
        logger.debug("Session lock unavailable (Redis): %s", e)
        return True


async def release_session_lock(session_id: str, owner_token: str) -> None:
    """Release the session lock only if this caller still owns it."""
    try:
        r = await _get_redis()
        if r is None:
            return
        key = f"session_lane:{session_id}"
        await r.eval(_RELEASE_IF_OWNER_SCRIPT, 1, key, owner_token)
    except Exception as e:
        logger.debug("Session lock release failed (Redis): %s", e)


async def renew_session_lock(
    session_id: str,
    owner_token: str,
    timeout: int = SESSION_LOCK_TTL_SECONDS,
) -> bool | None:
    """Extend the lease if this caller still owns it.

    Returns False when the lease is absent or owned by another request, True
    when extended, and None when Redis is unavailable (fail-open behavior).
    """
    try:
        r = await _get_redis()
        if r is None:
            return None
        key = f"session_lane:{session_id}"
        result = await r.eval(
            _RENEW_IF_OWNER_SCRIPT,
            1,
            key,
            owner_token,
            timeout * 1000,
        )
        return bool(result)
    except Exception as e:
        logger.debug("Session lock renewal unavailable (Redis): %s", e)
        return None


async def maintain_session_lock(
    session_id: str,
    owner_token: str,
    interval: float = SESSION_LOCK_RENEW_INTERVAL_SECONDS,
) -> None:
    """Renew a live session lease until canceled or ownership is lost."""
    while True:
        await asyncio.sleep(interval)
        renewed = await renew_session_lock(session_id, owner_token)
        if renewed is False:
            logger.warning("Session lock lease lost for session %s", session_id)
            return


async def session_lock_held(session_id: str) -> bool:
    """Return whether another turn currently owns the session lane."""
    try:
        r = await _get_redis()
        if r is None:
            return False
        return bool(await r.exists(f"session_lane:{session_id}"))
    except Exception as e:
        logger.debug("Session lock state unavailable (Redis): %s", e)
        return False


async def wait_for_session_lock(
    session_id: str,
    owner_token: str,
    poll_interval: float = 0.5,
    max_wait: float = 30,
) -> bool:
    """Wait until session lock is available, then acquire it.

    Returns True if lock acquired, False if timed out.
    Gracefully returns True if Redis is unavailable.
    """
    elapsed = 0.0
    while elapsed < max_wait:
        if await acquire_session_lock(session_id, owner_token=owner_token):
            return True
        await asyncio.sleep(poll_interval)
        elapsed += poll_interval
    return False
