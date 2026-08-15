"""LangGraph checkpointer selection and process lifecycle."""

from __future__ import annotations

import logging
import os
from typing import Any

from langgraph.checkpoint.memory import MemorySaver

from app.config import settings

logger = logging.getLogger(__name__)

_checkpointer: Any | None = None
_async_checkpointer: Any | None = None
_async_checkpointer_cm: Any | None = None


def create_checkpointer() -> Any:
    """Create the best available synchronous LangGraph checkpointer."""
    if (
        os.environ.get("AGENT_RUNTIME_STORE_BACKEND", "memory").strip().lower()
        == "sqlite"
        and _async_checkpointer is None
    ):
        try:
            from langgraph.checkpoint.sqlite import SqliteSaver

            context_manager = SqliteSaver.from_conn_string(
                settings.runtime_store_sqlite_path
            )
            checkpointer = context_manager.__enter__()
            checkpointer._cm_owner = context_manager  # type: ignore[attr-defined]
            checkpointer.setup()
            logger.info("Using SQLite LangGraph checkpointer")
            return checkpointer
        except ImportError:
            logger.warning(
                "langgraph-checkpoint-sqlite is not installed; trying other backends"
            )
        except Exception as error:  # noqa: BLE001 - backend fallback boundary
            logger.warning("SQLite LangGraph checkpointer unavailable: %s", error)

    redis_url = os.environ.get("REDIS_URL")
    if redis_url:
        try:
            from langgraph.checkpoint.redis import RedisSaver

            context_manager = RedisSaver.from_conn_string(redis_url)
            checkpointer = context_manager.__enter__()
            checkpointer._cm_owner = context_manager  # type: ignore[attr-defined]
            checkpointer.setup()
            logger.info("Using RedisSaver for conversation persistence (%s)", redis_url)
            return checkpointer
        except ImportError:
            logger.info("langgraph-checkpoint-redis not installed, trying postgres")
        except Exception as error:  # noqa: BLE001 - backend fallback boundary
            logger.warning("Redis unavailable (%s), trying postgres", error)

    postgres_url = os.environ.get("POSTGRES_URL")
    if postgres_url:
        try:
            from langgraph.checkpoint.postgres import PostgresSaver

            context_manager = PostgresSaver.from_conn_string(postgres_url)
            checkpointer = context_manager.__enter__()
            checkpointer._cm_owner = context_manager  # type: ignore[attr-defined]
            checkpointer.setup()
            logger.info("Using PostgresSaver for conversation persistence")
            return checkpointer
        except ImportError:
            logger.debug("langgraph-checkpoint-postgres not installed")
        except Exception as error:  # noqa: BLE001 - backend fallback boundary
            logger.debug("PostgreSQL unavailable: %s", error)

    logger.warning(
        "Using in-memory MemorySaver; set REDIS_URL or POSTGRES_URL for persistence"
    )
    return MemorySaver()


async def ensure_async_checkpointer() -> Any | None:
    """Enter AsyncSqliteSaver once before creating async LangGraph agents."""
    global _async_checkpointer, _async_checkpointer_cm
    if _async_checkpointer is not None:
        return _async_checkpointer
    if (
        os.environ.get("AGENT_RUNTIME_STORE_BACKEND", "memory").strip().lower()
        != "sqlite"
    ):
        return None

    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    checkpoint_path = settings.runtime_store_sqlite_path + ".langgraph"
    context_manager = AsyncSqliteSaver.from_conn_string(checkpoint_path)
    _async_checkpointer = await context_manager.__aenter__()
    _async_checkpointer_cm = context_manager
    await _async_checkpointer.setup()
    return _async_checkpointer


def get_checkpointer() -> Any:
    """Return the initialized async saver or lazily create a sync saver."""
    global _checkpointer
    if _async_checkpointer is not None:
        return _async_checkpointer
    if _checkpointer is None:
        _checkpointer = create_checkpointer()
    return _checkpointer