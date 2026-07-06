"""
Redis Air-Gap Storage Adapter.

Provides Air-Gap storage using Redis as the intermediate layer
between Baldur engine and business database.

Architecture:
    Business DB → (Business Layer writes) → Redis Air-Gap → (Engine reads) → Baldur
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from baldur.adapters.airgap.base import BaseAirGapAdapter
from baldur.utils.serialization import fast_dumps_str, fast_loads

if TYPE_CHECKING:
    import redis

logger = structlog.get_logger()


def _get_airgap_redis_ttl() -> int:
    """AirGapSettings에서 Redis TTL을 가져온다."""
    try:
        from baldur.settings.airgap import get_airgap_settings

        return get_airgap_settings().redis_ttl
    except Exception:
        return 3600  # 1 hour fallback


def _get_airgap_key_prefix() -> str:
    """AirGapSettings에서 키 접두사를 가져온다."""
    try:
        from baldur.settings.airgap import get_airgap_settings

        return get_airgap_settings().key_prefix
    except Exception:
        return "sh:airgap:"


class RedisAirGapAdapter(BaseAirGapAdapter):
    """
    Redis 기반 Air-Gap 저장소 어댑터.

    비즈니스 레이어에서 DB 변경 시 Redis에 요약 상태를 기록하고,
    Baldur 엔진은 Redis에서만 상태를 조회합니다.

    Features:
    - Atomic operations (INCR, DECR)
    - TTL support for automatic expiration
    - Batch read with MGET
    - JSON serialization for complex values

    Example:
        >>> import redis
        >>> client = redis.from_url("redis://localhost:6379/0")
        >>> adapter = RedisAirGapAdapter(client)
        >>>
        >>> # Business layer writes summary
        >>> adapter.write_summary("dlq:payment:pending", 5)
        >>>
        >>> # Baldur engine reads
        >>> count = adapter.read_summary("dlq:payment:pending")
        >>> print(count)  # 5
    """

    # 하위 호환성용 레거시 상수
    DEFAULT_TTL = 3600

    def __init__(
        self,
        redis_client: redis.Redis,
        prefix: str | None = None,
        default_ttl: int | None = None,
    ) -> None:
        """
        Initialize the Redis Air-Gap adapter.

        Args:
            redis_client: Redis client instance
            prefix: Key prefix for all Air-Gap keys (None = Settings에서 가져옴)
            default_ttl: Default TTL in seconds (None = Settings에서 가져옴)
        """
        # redis-py's stub declares dual sync/async return unions (`Awaitable[X] | X`)
        # for nearly every command. The Awaitable arm is unreachable on a sync
        # `redis.Redis`; widening to Any at the attribute keeps mypy out of every
        # call site (mirrors `core/state_backend.py:RedisStateBackend._client: Any`).
        self.redis: Any = redis_client
        self.prefix = prefix if prefix is not None else _get_airgap_key_prefix()
        self.default_ttl = (
            default_ttl if default_ttl is not None else _get_airgap_redis_ttl()
        )
        logger.info(
            "air_gap.redisairgapadapter_initialized",
            prefix=self.prefix,
        )

    def _make_key(self, key: str) -> str:
        """Create a Redis key with the configured prefix."""
        if key.startswith(self.prefix):
            return key
        return f"{self.prefix}{key}"

    def _serialize(self, value: Any) -> str:
        """Serialize value for Redis storage."""
        if isinstance(value, (str, int, float)):
            return str(value)
        return fast_dumps_str(value)

    def _deserialize(self, value: bytes | None) -> Any:
        """Deserialize value from Redis storage."""
        if value is None:
            return None

        str_value = value.decode("utf-8") if isinstance(value, bytes) else value

        # Try to parse as JSON first
        try:
            return fast_loads(str_value)
        except (ValueError, TypeError):
            # Return as string if not valid JSON
            return str_value

    def write_summary(self, key: str, value: Any, ttl: int | None = None) -> bool:
        """
        요약 상태를 Redis에 기록.

        Args:
            key: 저장소 키
            value: 저장할 값
            ttl: TTL in seconds (None = use default_ttl)

        Returns:
            성공 여부
        """
        try:
            redis_key = self._make_key(key)
            serialized = self._serialize(value)
            effective_ttl = ttl if ttl is not None else self.default_ttl

            if effective_ttl:
                self.redis.setex(redis_key, effective_ttl, serialized)
            else:
                self.redis.set(redis_key, serialized)

            logger.debug(
                "air_gap.written",
                redis_key=redis_key,
                written_value=value,
            )
            return True

        except Exception as e:
            logger.warning(
                "air_gap.write_failed",
                redis_key=key,
                error=e,
            )
            return False

    def read_summary(self, key: str) -> Any:
        """
        Redis에서 요약 상태 조회.

        Args:
            key: 저장소 키

        Returns:
            저장된 값 또는 None
        """
        try:
            redis_key = self._make_key(key)
            value = self.redis.get(redis_key)
            result = self._deserialize(value)
            logger.debug(
                "air_gap.read",
                redis_key=redis_key,
                read_result=result,
            )
            return result

        except Exception as e:
            logger.warning(
                "air_gap.read_failed",
                redis_key=key,
                error=e,
            )
            return None

    def delete_summary(self, key: str) -> bool:
        """
        Redis에서 요약 상태 삭제.

        Args:
            key: 저장소 키

        Returns:
            성공 여부
        """
        try:
            redis_key = self._make_key(key)
            self.redis.delete(redis_key)
            logger.debug(
                "air_gap.deleted",
                redis_key=redis_key,
            )
            return True

        except Exception as e:
            logger.warning(
                "air_gap.delete_failed",
                redis_key=key,
                error=e,
            )
            return False

    def read_many(self, keys: list[str]) -> dict[str, Any]:
        """
        여러 키의 값을 한 번에 조회 (MGET).

        Args:
            keys: 조회할 키 목록

        Returns:
            키-값 딕셔너리
        """
        if not keys:
            return {}

        try:
            redis_keys = [self._make_key(k) for k in keys]
            values = self.redis.mget(redis_keys)

            result = {}
            for key, value in zip(keys, values, strict=False):
                result[key] = self._deserialize(value)

            return result

        except Exception as e:
            logger.warning(
                "air_gap.read_many_failed",
                error=e,
            )
            return dict.fromkeys(keys)

    def increment(self, key: str, amount: int = 1) -> int:
        """
        카운터 값 증가 (atomic INCRBY).

        Args:
            key: 저장소 키
            amount: 증가량

        Returns:
            증가 후 값
        """
        try:
            redis_key = self._make_key(key)
            new_value = self.redis.incrby(redis_key, amount)

            # TTL 갱신
            if self.default_ttl:
                self.redis.expire(redis_key, self.default_ttl)

            logger.debug(
                "air_gap.incremented",
                redis_key=redis_key,
                amount=amount,
                new_value=new_value,
            )
            return new_value

        except Exception as e:
            logger.warning(
                "air_gap.increment_failed",
                redis_key=key,
                error=e,
            )
            return 0

    def decrement(self, key: str, amount: int = 1) -> int:
        """
        카운터 값 감소 (atomic, 음수 방지).

        Lua 스크립트를 사용하여 원자적으로 음수 방지를 보장합니다.

        Args:
            key: 저장소 키
            amount: 감소량

        Returns:
            감소 후 값 (최소 0)
        """
        # Lua script for atomic decrement with floor at 0
        lua_script = """
        local current = redis.call('GET', KEYS[1])
        if current == false then
            return 0
        end
        local new_value = tonumber(current) - tonumber(ARGV[1])
        if new_value < 0 then
            new_value = 0
        end
        redis.call('SET', KEYS[1], new_value)
        if tonumber(ARGV[2]) > 0 then
            redis.call('EXPIRE', KEYS[1], ARGV[2])
        end
        return new_value
        """

        try:
            redis_key = self._make_key(key)
            new_value = self.redis.eval(
                lua_script, 1, redis_key, amount, self.default_ttl or 0
            )
            logger.debug(
                "air_gap.decremented",
                redis_key=redis_key,
                amount=amount,
                new_value=new_value,
            )
            return int(new_value)

        except Exception as e:
            logger.warning(
                "air_gap.decrement_failed",
                redis_key=key,
                error=e,
            )
            return 0

    def is_enabled(self) -> bool:
        """
        Air-Gap 활성화 상태.

        Redis 연결이 정상이면 True.

        Returns:
            True if Redis is connected
        """
        try:
            self.redis.ping()
            return True
        except Exception:
            return False

    def health_check(self) -> dict[str, Any]:
        """
        Air-Gap 저장소 상태 확인.

        Returns:
            상태 정보 딕셔너리
        """
        try:
            self.redis.ping()
            info = self.redis.info("memory")
            return {
                "status": "healthy",
                "enabled": True,
                "prefix": self.prefix,
                "default_ttl": self.default_ttl,
                "used_memory": info.get("used_memory_human", "unknown"),
            }
        except Exception as e:
            return {
                "status": "unhealthy",
                "enabled": False,
                "error": str(e),
            }


__all__ = ["RedisAirGapAdapter"]
