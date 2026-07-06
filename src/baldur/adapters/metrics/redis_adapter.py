"""
Redis-based Metric Source Adapter.

Provides metrics from Redis cache using Write-Through pattern.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from baldur.adapters.metrics.base import BaseMetricSourceAdapter

if TYPE_CHECKING:
    import redis

# Sentinel referencing Any so the hook doesn't strip it as unused;
# `self.redis: Any` widening below relies on the runtime import.
_ANY_MARKER: Any = None

logger = structlog.get_logger()


class RedisMetricSourceAdapter(BaseMetricSourceAdapter):
    """
    Redis 기반 메트릭 소스 어댑터.

    Write-Through 패턴으로 비즈니스 로직에서 DB 저장 시
    Redis에도 동시에 기록하는 경우 사용.

    Example:
        >>> import redis
        >>> client = redis.from_url("redis://localhost:6379/0")
        >>> adapter = RedisMetricSourceAdapter(client)
        >>> # Write-Through: DB 저장과 동시에 Redis 업데이트
        >>> adapter.increment_dlq_pending("payment")
        >>> # 조회
        >>> count = adapter.get_dlq_pending_count("payment")
    """

    def __init__(
        self,
        redis_client: redis.Redis,
        prefix: str = "sh:metrics:",
    ):
        """
        Initialize the Redis adapter.

        Args:
            redis_client: Redis client instance
            prefix: Key prefix for all metric keys
        """
        # redis-py stub declares dual sync/async return unions; widening to
        # Any at the attribute keeps mypy out of sync call sites (mirrors
        # adapters/airgap/redis_adapter.py).
        self.redis: Any = redis_client
        self.prefix = prefix

    def _make_key(self, *parts: str) -> str:
        """Create a Redis key with the configured prefix."""
        return f"{self.prefix}{':'.join(parts)}"

    def get_dlq_pending_count(self, domain: str) -> int:
        """
        도메인별 대기 중인 DLQ 항목 수 반환.

        Args:
            domain: 도메인 이름 (payment, point, inventory 등)

        Returns:
            대기 중인 DLQ 항목 수
        """
        try:
            key = self._make_key("dlq", "pending", domain)
            value = self.redis.get(key)
            return int(value) if value else 0
        except Exception as e:
            logger.warning(
                "redis_adapter.get_dlq_pending_failed",
                error=e,
            )
            return 0

    def get_dlq_count_by_status(self, status: str) -> int:
        """
        상태별 DLQ 항목 수 반환.

        Args:
            status: 상태 (pending, resolved, failed 등)

        Returns:
            해당 상태의 DLQ 항목 수
        """
        try:
            key = self._make_key("dlq", "status", status)
            value = self.redis.get(key)
            return int(value) if value else 0
        except Exception as e:
            logger.warning(
                "redis_adapter.get_dlq_count_failed",
                error=e,
            )
            return 0

    def get_circuit_breaker_state(self, service: str) -> str:
        """
        서비스의 Circuit Breaker 상태 반환.

        Args:
            service: 서비스 이름

        Returns:
            상태 문자열 (closed, open, half_open)
        """
        try:
            key = self._make_key("cb", "state", service)
            value = self.redis.get(key)
            if value:
                # Handle both bytes and string
                if isinstance(value, bytes):
                    return value.decode("utf-8")
                return str(value)
            return "closed"
        except Exception as e:
            logger.warning(
                "redis_adapter.get_cb_state_failed",
                error=e,
            )
            return "closed"

    def get_retry_success_rate(self, domain: str) -> float:
        """
        도메인별 재시도 성공률 반환.

        Args:
            domain: 도메인 이름

        Returns:
            성공률 (0.0 ~ 100.0)
        """
        try:
            key = self._make_key("retry", "success_rate", domain)
            value = self.redis.get(key)
            return float(value) if value else 0.0
        except Exception as e:
            logger.warning(
                "redis_adapter.get_retry_success_failed",
                error=e,
            )
            return 0.0

    # =========================================================================
    # Write-Through Helper Methods
    # =========================================================================

    def increment_dlq_pending(self, domain: str) -> int:
        """
        DLQ 대기 수 증가 (DLQ 생성 시 호출).

        Args:
            domain: 도메인 이름

        Returns:
            증가 후 값
        """
        try:
            key = self._make_key("dlq", "pending", domain)
            return self.redis.incr(key)
        except Exception as e:
            logger.warning(
                "redis_adapter.increment_dlq_pending_failed",
                error=e,
            )
            return 0

    def decrement_dlq_pending(self, domain: str) -> int:
        """
        DLQ 대기 수 감소 (DLQ 해결 시 호출).

        Args:
            domain: 도메인 이름

        Returns:
            감소 후 값
        """
        try:
            key = self._make_key("dlq", "pending", domain)
            return self.redis.decr(key)
        except Exception as e:
            logger.warning(
                "redis_adapter.decrement_dlq_pending_failed",
                error=e,
            )
            return 0

    def set_circuit_breaker_state(
        self,
        service: str,
        state: str,
        ttl_seconds: int | None = None,
    ) -> None:
        """
        Circuit Breaker 상태 설정.

        Args:
            service: 서비스 이름
            state: 상태 (closed, open, half_open)
            ttl_seconds: Optional TTL in seconds
        """
        try:
            key = self._make_key("cb", "state", service)
            if ttl_seconds:
                self.redis.setex(key, ttl_seconds, state)
            else:
                self.redis.set(key, state)
        except Exception as e:
            logger.warning(
                "redis_adapter.set_cb_state_failed",
                error=e,
            )

    def set_retry_success_rate(self, domain: str, rate: float) -> None:
        """
        재시도 성공률 설정.

        Args:
            domain: 도메인 이름
            rate: 성공률 (0.0 ~ 100.0)
        """
        try:
            key = self._make_key("retry", "success_rate", domain)
            self.redis.set(key, str(rate))
        except Exception as e:
            logger.warning(
                "redis_adapter.set_retry_success_failed",
                error=e,
            )

    def set_dlq_pending_count(self, domain: str, count: int) -> None:
        """
        DLQ 대기 수 직접 설정 (동기화 시 사용).

        Args:
            domain: 도메인 이름
            count: 설정할 값
        """
        try:
            key = self._make_key("dlq", "pending", domain)
            self.redis.set(key, str(count))
        except Exception as e:
            logger.warning(
                "redis_adapter.set_dlq_pending_failed",
                error=e,
            )


__all__ = ["RedisMetricSourceAdapter"]
