"""File watcher for agent configs — hot reload on change."""

import asyncio
import logging
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)


async def watch_configs(config_dir: str, on_change: Callable[[str], None]) -> None:
    """Watch agent config directory for YAML changes.

    Uses watchfiles if available, otherwise falls back to polling.
    Runs indefinitely — meant to be launched as an asyncio task.
    Gracefully handles missing directory or import errors.
    """
    path = Path(config_dir)
    if not path.exists():
        logger.info("Config dir %s does not exist, skipping watch", config_dir)
        return

    try:
        from watchfiles import awatch

        logger.info("Watching %s for config changes (watchfiles)", config_dir)
        async for changes in awatch(config_dir):
            for _change_type, changed_path in changes:
                if changed_path.endswith(".yaml"):
                    logger.info("Config changed: %s", changed_path)
                    try:
                        on_change(changed_path)
                    except Exception as e:
                        logger.error("Error handling config change: %s", e)
    except ImportError:
        logger.info(
            "watchfiles not installed, using polling fallback for %s", config_dir
        )
        await _poll_configs(config_dir, on_change)
    except Exception as e:
        logger.error("Config watcher failed: %s", e)


async def _poll_configs(
    config_dir: str, on_change: Callable[[str], None], interval: float = 5.0
) -> None:
    """Fallback polling watcher when watchfiles is not installed."""
    path = Path(config_dir)
    mtimes: dict[str, float] = {}

    # Initial scan
    for f in path.glob("*.yaml"):
        mtimes[str(f)] = f.stat().st_mtime

    while True:
        await asyncio.sleep(interval)
        try:
            for f in path.glob("*.yaml"):
                fpath = str(f)
                mtime = f.stat().st_mtime
                if fpath not in mtimes or mtimes[fpath] < mtime:
                    mtimes[fpath] = mtime
                    logger.info("Config changed (poll): %s", fpath)
                    try:
                        on_change(fpath)
                    except Exception as e:
                        logger.error("Error handling config change: %s", e)
        except Exception as e:
            logger.debug("Poll error: %s", e)
