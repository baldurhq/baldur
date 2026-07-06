"""
RedisConfigHistoryStore — Redis implementation of ConfigHistoryStore.

Uses Redis List, String, and Pipeline for atomic version management.
Preserves existing key structure from ConfigHistoryService.
"""

from __future__ import annotations

from typing import Any

import structlog

from baldur.core.exceptions import StoreError
from baldur.interfaces.config_history_store import ConfigHistoryStore
from baldur.utils.serialization import fast_dumps_str, fast_loads

logger = structlog.get_logger()

__all__ = ["RedisConfigHistoryStore"]


class RedisConfigHistoryStore(ConfigHistoryStore):
    """Redis-backed config history store.

    Uses the same key structure as the original ConfigHistoryService:
    - {prefix}config:history:{config_type} — List (history, newest first)
    - {prefix}config:version:{config_type} — String (version counter)
    - {prefix}config:current:{config_type} — String (current version JSON)

    Key prefix comes from config_history/keys.py helpers.
    """

    def __init__(self, redis_client: Any) -> None:
        self._redis = redis_client

    def next_version(self, config_type: str) -> int:
        from baldur.services.config_history.keys import _get_config_version_key

        version_key = _get_config_version_key(config_type)
        try:
            return self._redis.incr(version_key)
        except Exception as e:
            raise StoreError(f"Failed to increment version for {config_type}") from e

    def save_version(
        self,
        config_type: str,
        version_data: dict[str, Any],
        max_entries: int,
    ) -> None:
        from baldur.services.config_history.keys import (
            _get_config_current_key,
            _get_config_history_key,
        )

        history_key = _get_config_history_key(config_type)
        current_key = _get_config_current_key(config_type)
        serialized = fast_dumps_str(version_data)

        try:
            pipe = self._redis.pipeline()
            pipe.lpush(history_key, serialized)
            pipe.ltrim(history_key, 0, max_entries - 1)
            pipe.set(current_key, serialized)
            pipe.execute()
        except Exception as e:
            raise StoreError(f"Failed to save version for {config_type}") from e

    def get_current(self, config_type: str) -> dict[str, Any] | None:
        from baldur.services.config_history.keys import _get_config_current_key

        current_key = _get_config_current_key(config_type)
        try:
            data = self._redis.get(current_key)
            if data is None:
                return None
            if isinstance(data, bytes):
                data = data.decode("utf-8")
            return fast_loads(data)
        except Exception as e:
            logger.warning(
                "redis_config_history_store.get_current_failed",
                config_type=config_type,
                error=e,
            )
            return None

    def get_history(self, config_type: str, limit: int) -> list[dict[str, Any]]:
        from baldur.services.config_history.keys import _get_config_history_key

        history_key = _get_config_history_key(config_type)
        try:
            entries = self._redis.lrange(history_key, 0, limit - 1)
            result = []
            for entry in entries:
                if isinstance(entry, bytes):
                    entry = entry.decode("utf-8")
                result.append(fast_loads(entry))
            return result
        except Exception as e:
            logger.warning(
                "redis_config_history_store.get_history_failed",
                config_type=config_type,
                error=e,
            )
            return []

    def get_version_count(self, config_type: str) -> int:
        from baldur.services.config_history.keys import _get_config_history_key

        history_key = _get_config_history_key(config_type)
        try:
            return self._redis.llen(history_key)
        except Exception:
            return 0

    def clear(self, config_type: str) -> None:
        from baldur.services.config_history.keys import (
            _get_config_current_key,
            _get_config_history_key,
            _get_config_version_key,
        )

        history_key = _get_config_history_key(config_type)
        version_key = _get_config_version_key(config_type)
        current_key = _get_config_current_key(config_type)

        try:
            pipe = self._redis.pipeline()
            pipe.delete(history_key)
            pipe.delete(version_key)
            pipe.delete(current_key)
            pipe.execute()
        except Exception as e:
            raise StoreError(f"Failed to clear history for {config_type}") from e
