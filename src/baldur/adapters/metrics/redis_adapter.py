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
    Redis-backed metric source adapter.

    Use this when business logic follows the Write-Through pattern and updates
    Redis at the same time it writes to the DB.

    Example:
        >>> import redis
        >>> client = redis.from_url("redis://localhost:6379/0")
        >>> adapter = RedisMetricSourceAdapter(client)
        >>> # Write-Through: update Redis together with the DB write
        >>> adapter.increment_dlq_pending("payment")
        >>> # Read back
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
        # the Air-Gap Redis adapter).
        self.redis: Any = redis_client
        self.prefix = prefix

    def _make_key(self, *parts: str) -> str:
        """Create a Redis key with the configured prefix."""
        return f"{self.prefix}{':'.join(parts)}"

    def get_dlq_pending_count(self, domain: str) -> int:
        """
        Return the number of pending DLQ entries for a domain.

        Args:
            domain: domain name (payment, point, inventory, etc.)

        Returns:
            Number of pending DLQ entries
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
        Return the number of DLQ entries in a given status.

        Args:
            status: status (pending, resolved, failed, etc.)

        Returns:
            Number of DLQ entries in that status
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
        Return the Circuit Breaker state of a service.

        Args:
            service: service name

        Returns:
            State string (closed, open, half_open)
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
        Return the retry success rate for a domain.

        Args:
            domain: domain name

        Returns:
            Success rate (0.0 ~ 100.0)
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
        Increment the DLQ pending count (called when a DLQ entry is created).

        Args:
            domain: domain name

        Returns:
            The value after incrementing
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
        Decrement the DLQ pending count (called when a DLQ entry is resolved).

        Args:
            domain: domain name

        Returns:
            The value after decrementing
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
        Set the Circuit Breaker state.

        Args:
            service: service name
            state: state (closed, open, half_open)
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
        Set the retry success rate.

        Args:
            domain: domain name
            rate: success rate (0.0 ~ 100.0)
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
        Set the DLQ pending count directly (used when syncing).

        Args:
            domain: domain name
            count: value to set
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
