"""
RedisCanaryRolloutStore — Redis implementation of CanaryRolloutStore.

Preserves existing key patterns from CanaryRolloutService and CanaryConfigLock.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import structlog

from baldur.interfaces.canary_rollout_store import CanaryRolloutStore
from baldur.settings.namespace import get_key_prefix
from baldur.utils.serialization import fast_dumps_str, fast_loads

logger = structlog.get_logger()

__all__ = ["RedisCanaryRolloutStore"]

# Key templates (match existing CanaryRolloutService patterns)
_ROLLOUT_KEY = "{prefix}canary:rollout:{rollout_id}"
_ACTIVE_KEY = "{prefix}canary:active"
_LOCK_KEY = "{prefix}canary:lock:{config_type}"

# Default lock timeout
_DEFAULT_LOCK_TIMEOUT = timedelta(minutes=30)


_CAS_SCRIPT = """
local current = redis.call("get", KEYS[1])
if current == false then
    return -1
end
local stored = cjson.decode(current)
if stored["version"] == tonumber(ARGV[3]) then
    redis.call("set", KEYS[1], ARGV[1], "EX", ARGV[2])
    return 1
end
return 0
"""


class RedisCanaryRolloutStore(CanaryRolloutStore):
    """Redis-backed canary rollout store with config lock support."""

    def __init__(self, redis_client: Any) -> None:
        self._redis = redis_client

    # -- Rollout CRUD ---------------------------------------------------------

    def get_rollout(self, rollout_id: str) -> dict[str, Any] | None:
        key = _ROLLOUT_KEY.format(prefix=get_key_prefix(), rollout_id=rollout_id)
        try:
            data = self._redis.get(key)
            if data is None:
                return None
            if isinstance(data, bytes):
                data = data.decode("utf-8")
            return fast_loads(data)
        except Exception as e:
            logger.warning("redis_canary_rollout_store.get_rollout_failed", error=e)
            return None

    def save_rollout(
        self,
        rollout_id: str,
        data: dict[str, Any],
        ttl_seconds: int,
        expected_version: int | None = None,
    ) -> bool:
        key = _ROLLOUT_KEY.format(prefix=get_key_prefix(), rollout_id=rollout_id)
        try:
            if expected_version is None:
                # Unconditional save: create_rollout(), legacy paths
                self._redis.set(key, fast_dumps_str(data), ex=ttl_seconds)
                return True

            # CAS save: promote(), rollback(), pause(), resume(), start_rollout()
            result = self._redis.eval(
                _CAS_SCRIPT,
                1,
                key,
                fast_dumps_str(data),
                ttl_seconds,
                expected_version,
            )
            if result == 1:
                return True
            if result == -1:
                logger.warning(
                    "redis_canary_rollout_store.cas_key_not_found",
                    rollout_id=rollout_id,
                    expected_version=expected_version,
                )
            else:
                logger.warning(
                    "redis_canary_rollout_store.cas_version_conflict",
                    rollout_id=rollout_id,
                    expected_version=expected_version,
                )
            return False
        except Exception as e:
            logger.warning("redis_canary_rollout_store.save_rollout_failed", error=e)
            return False

    # -- Active set -----------------------------------------------------------

    def get_active_ids(self) -> set[str]:
        key = _ACTIVE_KEY.format(prefix=get_key_prefix())
        try:
            members = self._redis.smembers(key)
            return {m.decode("utf-8") if isinstance(m, bytes) else m for m in members}
        except Exception as e:
            logger.warning("redis_canary_rollout_store.get_active_ids_failed", error=e)
            return set()

    def add_active(self, rollout_id: str) -> None:
        key = _ACTIVE_KEY.format(prefix=get_key_prefix())
        try:
            self._redis.sadd(key, rollout_id)
        except Exception as e:
            logger.warning("redis_canary_rollout_store.add_active_failed", error=e)

    def remove_active(self, rollout_id: str) -> None:
        key = _ACTIVE_KEY.format(prefix=get_key_prefix())
        try:
            self._redis.srem(key, rollout_id)
        except Exception as e:
            logger.warning("redis_canary_rollout_store.remove_active_failed", error=e)

    # -- Completed rollout search ---------------------------------------------

    def find_completed(self, pattern: str) -> list[dict[str, Any]]:
        results = []
        try:
            cursor = 0
            while True:
                cursor, keys = self._redis.scan(cursor, match=pattern, count=100)
                for key in keys:
                    if isinstance(key, bytes):
                        key = key.decode("utf-8")
                    data = self._redis.get(key)
                    if data:
                        if isinstance(data, bytes):
                            data = data.decode("utf-8")
                        results.append(fast_loads(data))
                if cursor == 0:
                    break
        except Exception as e:
            logger.warning("redis_canary_rollout_store.find_completed_failed", error=e)
        return results

    # -- Config lock ----------------------------------------------------------

    def acquire_config_lock(
        self,
        config_type: str,
        rollout_id: str,
        timeout: timedelta | None = None,
    ) -> bool:
        lock_timeout = timeout or _DEFAULT_LOCK_TIMEOUT
        lock_key = _LOCK_KEY.format(prefix=get_key_prefix(), config_type=config_type)
        timeout_ms = int(lock_timeout.total_seconds() * 1000)

        try:
            acquired = self._redis.set(lock_key, rollout_id, nx=True, px=timeout_ms)
            if acquired:
                logger.info(
                    "redis_canary_rollout_store.lock_acquired",
                    config_type=config_type,
                    rollout_id=rollout_id,
                )
            return bool(acquired)
        except Exception as e:
            logger.warning("redis_canary_rollout_store.acquire_lock_failed", error=e)
            return False

    def release_config_lock(self, config_type: str, rollout_id: str) -> bool:
        lock_key = _LOCK_KEY.format(prefix=get_key_prefix(), config_type=config_type)
        lua_script = """
        if redis.call("get", KEYS[1]) == ARGV[1] then
            return redis.call("del", KEYS[1])
        else
            return 0
        end
        """
        try:
            result = self._redis.eval(lua_script, 1, lock_key, rollout_id)
            if result == 1:
                logger.info(
                    "redis_canary_rollout_store.lock_released",
                    config_type=config_type,
                    rollout_id=rollout_id,
                )
                return True
            return False
        except Exception as e:
            logger.exception("redis_canary_rollout_store.release_lock_error", error=e)
            return False

    def get_config_lock_owner(self, config_type: str) -> str | None:
        lock_key = _LOCK_KEY.format(prefix=get_key_prefix(), config_type=config_type)
        try:
            owner = self._redis.get(lock_key)
            if owner and isinstance(owner, bytes):
                owner = owner.decode("utf-8")
            return owner
        except Exception:
            return None

    def is_config_locked(self, config_type: str) -> bool:
        lock_key = _LOCK_KEY.format(prefix=get_key_prefix(), config_type=config_type)
        try:
            return self._redis.exists(lock_key) > 0
        except Exception:
            return False

    def force_release_config_lock(self, config_type: str) -> bool:
        lock_key = _LOCK_KEY.format(prefix=get_key_prefix(), config_type=config_type)
        try:
            result = self._redis.delete(lock_key)
            logger.warning(
                "redis_canary_rollout_store.force_lock_released",
                config_type=config_type,
            )
            return result > 0
        except Exception as e:
            logger.exception("redis_canary_rollout_store.force_release_error", error=e)
            return False

    def extend_config_lock(
        self,
        config_type: str,
        rollout_id: str,
        additional_time: timedelta | None = None,
    ) -> bool:
        lock_key = _LOCK_KEY.format(prefix=get_key_prefix(), config_type=config_type)
        extend_time = additional_time or _DEFAULT_LOCK_TIMEOUT
        extend_ms = int(extend_time.total_seconds() * 1000)

        lua_script = """
        if redis.call("get", KEYS[1]) == ARGV[1] then
            return redis.call("pexpire", KEYS[1], ARGV[2])
        else
            return 0
        end
        """
        try:
            result = self._redis.eval(lua_script, 1, lock_key, rollout_id, extend_ms)
            return result == 1
        except Exception as e:
            logger.exception("redis_canary_rollout_store.extend_lock_error", error=e)
            return False
