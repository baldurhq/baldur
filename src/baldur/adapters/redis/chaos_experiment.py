"""
RedisChaosExperimentStore — Redis implementation of ChaosExperimentStore.

Preserves existing key pattern: *:chaos:experiment:*
"""

from __future__ import annotations

from typing import Any

import structlog

from baldur.interfaces.chaos_experiment_store import ChaosExperimentStore
from baldur.settings.namespace import get_key_prefix
from baldur.utils.serialization import fast_dumps_str, fast_loads

logger = structlog.get_logger()

__all__ = ["RedisChaosExperimentStore"]

_EXPERIMENT_KEY = "{prefix}chaos:experiment:{experiment_id}"


class RedisChaosExperimentStore(ChaosExperimentStore):
    """Redis-backed chaos experiment store."""

    def __init__(self, redis_client: Any) -> None:
        self._redis = redis_client

    def save(
        self,
        experiment_id: str,
        data: dict[str, Any],
        ttl_seconds: int,
    ) -> None:
        key = _EXPERIMENT_KEY.format(
            prefix=get_key_prefix(), experiment_id=experiment_id
        )
        try:
            self._redis.set(key, fast_dumps_str(data), ex=ttl_seconds)
        except Exception as e:
            logger.warning(
                "redis_chaos_experiment_store.save_failed",
                experiment_id=experiment_id,
                error=e,
            )

    def get(self, experiment_id: str) -> dict[str, Any] | None:
        key = _EXPERIMENT_KEY.format(
            prefix=get_key_prefix(), experiment_id=experiment_id
        )
        try:
            data = self._redis.get(key)
            if data is None:
                return None
            if isinstance(data, bytes):
                data = data.decode("utf-8")
            return fast_loads(data)
        except Exception as e:
            logger.warning(
                "redis_chaos_experiment_store.get_failed",
                experiment_id=experiment_id,
                error=e,
            )
            return None

    def delete(self, experiment_id: str) -> None:
        key = _EXPERIMENT_KEY.format(
            prefix=get_key_prefix(), experiment_id=experiment_id
        )
        try:
            self._redis.delete(key)
        except Exception as e:
            logger.warning(
                "redis_chaos_experiment_store.delete_failed",
                experiment_id=experiment_id,
                error=e,
            )

    def find_active(self) -> list[dict[str, Any]]:
        pattern = _EXPERIMENT_KEY.format(prefix=get_key_prefix(), experiment_id="*")
        results = []
        try:
            cursor = 0
            while True:
                cursor, keys = self._redis.scan(cursor, match=pattern, count=100)
                for key in keys:
                    try:
                        data = self._redis.get(key)
                        if data:
                            if isinstance(data, bytes):
                                data = data.decode("utf-8")
                            exp_data = fast_loads(data)
                            if exp_data.get("status") == "active":
                                results.append(exp_data)
                    except Exception as e:
                        logger.debug(
                            "redis_chaos_experiment_store.parse_experiment_failed",
                            error=e,
                        )
                if cursor == 0:
                    break
        except Exception as e:
            logger.warning("redis_chaos_experiment_store.find_active_failed", error=e)
        return results
