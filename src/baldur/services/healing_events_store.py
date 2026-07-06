"""
Healing Events Redis Store.

Stores Healing Events in Redis to support data synchronization across
multiple workers. Entries auto-expire after a TTL of 7 days, and an
In-Memory cache is also maintained.

Key Pattern:
    baldur:events:{YYYY-MM-DD} - per-date event List

Features:
    - Stores events to Redis (LPUSH)
    - Auto-sets a TTL of 7 days
    - In-Memory fallback (when Redis fails)
    - Cross-worker synchronization
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from typing import Any

import structlog

from baldur.utils.serialization import fast_dumps_str, fast_loads

logger = structlog.get_logger()


# =============================================================================
# Constants
# =============================================================================

EVENTS_KEY_PREFIX = "baldur:events"
EVENTS_TTL_DAYS = 7
EVENTS_TTL_SECONDS = EVENTS_TTL_DAYS * 24 * 60 * 60  # 604800 seconds

# In-Memory fallback (used when Redis fails)
_events_memory_lock = threading.Lock()
_events_memory: list[dict[str, Any]] = []
_max_events_memory = 500

# Redis-usage flag (can be disabled in tests)
_redis_enabled = True


# =============================================================================
# Configuration
# =============================================================================


def set_redis_events_enabled(enabled: bool) -> None:
    """Enable or disable Redis event storage."""
    global _redis_enabled
    _redis_enabled = enabled


def get_redis_events_enabled() -> bool:
    """Whether Redis event storage is enabled."""
    return _redis_enabled


# =============================================================================
# Redis Client Helper
# =============================================================================


def _get_redis_client() -> Any | None:
    """Get the Redis client."""
    if not _redis_enabled:
        return None

    try:
        from baldur.adapters.redis import get_redis_client

        return get_redis_client()
    except ImportError:
        return None
    except Exception as e:
        logger.debug(
            "healing_events.redis_client_unavailable",
            error=e,
        )
        return None


def _get_today_key() -> str:
    """Build today's Redis key."""
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    return f"{EVENTS_KEY_PREFIX}:{today}"


def _get_date_key(date_str: str) -> str:
    """Build the Redis key for a given date."""
    return f"{EVENTS_KEY_PREFIX}:{date_str}"


# =============================================================================
# Event Storage Functions
# =============================================================================


def add_healing_event_redis(event: dict[str, Any]) -> bool:
    """
    Store a Healing Event in Redis.

    Uses Redis LPUSH to prepend to the list and sets a TTL of 7 days.
    Falls back to In-Memory when Redis fails.

    Args:
        event: Event data dictionary

    Returns:
        Whether the Redis store succeeded
    """
    global _events_memory

    # Add timestamp
    if "recorded_at" not in event:
        event["recorded_at"] = datetime.now(UTC).isoformat()

    # Try Redis store
    redis_client = _get_redis_client()
    if redis_client:
        try:
            key = _get_today_key()
            event_json = fast_dumps_str(event, default=str)

            # Prepend to list with LPUSH
            redis_client.lpush(key, event_json)

            # Set TTL (only when the key is first created)
            if redis_client.ttl(key) < 0:
                redis_client.expire(key, EVENTS_TTL_SECONDS)

            logger.debug(
                "healing_events.event_saved_redis",
                event_store_key=key,
            )
            return True

        except Exception as e:
            logger.warning(
                "healing_events.redis_save_failed",
                error=e,
            )

    # In-Memory fallback
    with _events_memory_lock:
        _events_memory.append(event)
        if len(_events_memory) > _max_events_memory:
            # Delete old events in place
            del _events_memory[: len(_events_memory) - _max_events_memory]

    logger.debug("healing_events.event_saved_memory_fallback")
    return False


def get_healing_events_redis(
    limit: int = 50,
    days_back: int = 1,
) -> list[dict[str, Any]]:
    """
    Read Healing Events from Redis.

    Reads events for the last N days.
    Falls back to In-Memory when Redis fails.

    Args:
        limit: Maximum number of events to return
        days_back: Number of past days to query (default 1 = today only)

    Returns:
        List of event dictionaries (newest first)
    """
    redis_client = _get_redis_client()

    if redis_client:
        try:
            events = []
            today = datetime.now(UTC)

            # Iterate the past dates within the requested range
            for day_offset in range(days_back):
                if len(events) >= limit:
                    break

                date = today.replace(hour=0, minute=0, second=0, microsecond=0)
                date = date.replace(day=today.day - day_offset)
                date_str = date.strftime("%Y-%m-%d")
                key = _get_date_key(date_str)

                # LRANGE query (newest first)
                remaining = limit - len(events)
                raw_events = redis_client.lrange(key, 0, remaining - 1)

                for raw in raw_events:
                    try:
                        if isinstance(raw, bytes):
                            raw = raw.decode("utf-8")
                        events.append(fast_loads(raw))
                    except (ValueError, UnicodeDecodeError) as e:
                        logger.warning(
                            "healing_events.parse_event_failed",
                            error=e,
                        )

            return events[:limit]

        except Exception as e:
            logger.warning(
                "healing_events.redis_query_failed",
                error=e,
            )

    # In-Memory fallback
    with _events_memory_lock:
        return list(_events_memory[-limit:])


def get_healing_events_count_redis(days_back: int = 1) -> int:
    """
    Read the total Healing Events count.

    Args:
        days_back: Number of past days to query

    Returns:
        Total number of events
    """
    redis_client = _get_redis_client()

    if redis_client:
        try:
            total = 0
            today = datetime.now(UTC)

            for day_offset in range(days_back):
                date = today.replace(hour=0, minute=0, second=0, microsecond=0)
                date = date.replace(day=today.day - day_offset)
                date_str = date.strftime("%Y-%m-%d")
                key = _get_date_key(date_str)

                count = redis_client.llen(key)
                total += count

            return total

        except Exception as e:
            logger.warning(
                "healing_events.redis_count_failed",
                error=e,
            )

    # In-Memory fallback
    with _events_memory_lock:
        return len(_events_memory)


def clear_healing_events_redis() -> int:
    """
    Clear Healing Events (for tests).

    Clears both Redis and In-Memory storage.

    Returns:
        Number of events cleared
    """
    count = 0

    # Clear In-Memory
    with _events_memory_lock:
        count = len(_events_memory)
        _events_memory.clear()  # Clear while keeping the existing list object

    # Clear Redis (today's key only)
    redis_client = _get_redis_client()
    if redis_client:
        try:
            key = _get_today_key()
            redis_count = redis_client.llen(key)
            redis_client.delete(key)
            count = max(count, redis_count)
        except Exception as e:
            logger.warning(
                "healing_events.redis_clear_failed",
                error=e,
            )

    return count


# =============================================================================
# Module Exports
# =============================================================================

__all__ = [
    "add_healing_event_redis",
    "get_healing_events_redis",
    "get_healing_events_count_redis",
    "clear_healing_events_redis",
    "set_redis_events_enabled",
    "get_redis_events_enabled",
    "EVENTS_KEY_PREFIX",
    "EVENTS_TTL_DAYS",
    "EVENTS_TTL_SECONDS",
]
