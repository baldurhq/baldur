"""
Redis Health Checker — Mini Circuit Breaker pattern.

Monitors Redis connection health with periodic pings,
automatic failover detection, and jitter-based recovery
to prevent thundering herd on Redis recovery.

Extracted from api/django/rate_limit.py as part of 358 rate_limit package split.
"""

from __future__ import annotations

import random
import threading
import time
from enum import Enum

import structlog

from baldur.api.django.rate_limit.config import _get_metrics, _get_setting

logger = structlog.get_logger()

__all__ = ["RedisHealthState", "RedisHealthChecker"]


class RedisHealthState(str, Enum):
    """Redis health states."""

    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"
    RECOVERING = "recovering"


class RedisHealthChecker:
    """
    Redis health checker with Mini Circuit Breaker pattern.

    Features:
    - Periodic ping to check Redis status
    - Circuit breaker to stop connection attempts after failures
    - Jitter-based recovery to prevent thundering herd
    - Dedicated low-timeout connection for health checks

    Settings are loaded from ApiRateLimitSettings.
    """

    def __init__(self):
        self._state: RedisHealthState = RedisHealthState.HEALTHY
        self._consecutive_failures = 0
        self._last_check_time = 0.0
        self._recovery_time: float | None = None
        self._redis_client = None
        self._health_ping_client = None
        self._lock = threading.Lock()

    @property
    def ping_interval(self) -> int:
        """Redis health check interval (seconds)."""
        return int(_get_setting("redis_ping_interval", 5))

    @property
    def failure_threshold(self) -> int:
        """Consecutive failures before UNHEALTHY transition."""
        return int(_get_setting("redis_failure_threshold", 3))

    @property
    def recovery_jitter_max(self) -> int:
        """Max jitter for thundering herd prevention on recovery (seconds)."""
        return int(_get_setting("redis_recovery_jitter_max", 10))

    @property
    def ping_timeout_ms(self) -> int:
        """Health check ping timeout (milliseconds)."""
        return int(_get_setting("redis_ping_timeout_ms", 100))

    @property
    def is_healthy(self) -> bool:
        """Check if Redis is available."""
        return bool(self._state == RedisHealthState.HEALTHY)

    @property
    def is_degraded(self) -> bool:
        """Check if operating in degraded mode."""
        return bool(self._state != RedisHealthState.HEALTHY)

    @property
    def state(self) -> RedisHealthState:
        """Get current state."""
        return self._state

    def check_health(self) -> bool:
        """
        Perform Redis health check.

        Returns:
            True if Redis is healthy
        """
        now = time.time()

        # Rate limit health checks
        if now - self._last_check_time < self.ping_interval:
            return self.is_healthy

        with self._lock:
            self._last_check_time = now

            try:
                if self._redis_client is None:
                    self._redis_client = self._get_redis_client()

                if self._redis_client is None:
                    # Redis not configured
                    return self._handle_no_redis()

                # PING test using dedicated low-timeout client
                self._get_health_ping_client().ping()

                # Handle recovery states
                if self._state == RedisHealthState.UNHEALTHY:
                    self._initiate_recovery()
                elif self._state == RedisHealthState.RECOVERING:
                    self._complete_recovery_if_ready()
                else:
                    self._consecutive_failures = 0

                return self.is_healthy

            except Exception as e:
                self._handle_failure(e)
                return False

    def _get_health_ping_client(self):
        """Get or create a dedicated low-timeout Redis client for health checks.

        Note: Creates StrictRedis directly from connection_kwargs to preserve
        Django cache server affinity. In Sentinel environments, this client
        may not automatically follow master failover. This is acceptable as
        the health check interval ensures eventual recovery detection.
        """
        if self._health_ping_client is not None:
            return self._health_ping_client

        try:
            timeout_sec = self.ping_timeout_ms / 1000.0
            # Create a copy of the main client with lower socket_timeout
            conn_kwargs = self._redis_client.connection_pool.connection_kwargs.copy()
            conn_kwargs["socket_timeout"] = timeout_sec
            conn_kwargs["socket_connect_timeout"] = timeout_sec

            import redis

            self._health_ping_client = redis.StrictRedis(**conn_kwargs)
            return self._health_ping_client
        except Exception:
            # Fall back to main client if dedicated client creation fails
            return self._redis_client

    def _handle_no_redis(self) -> bool:
        """Handle case where Redis is not configured."""
        if self._state != RedisHealthState.UNHEALTHY:
            logger.info("redis_health.local_rate_limiter_fallback")
            self._state = RedisHealthState.UNHEALTHY
            self._record_degraded_mode(True)
        return False

    def _handle_failure(self, error: Exception):
        """Handle Redis connection failure."""
        self._consecutive_failures += 1

        if (
            self._consecutive_failures >= self.failure_threshold
            and self._state != RedisHealthState.UNHEALTHY
        ):
            self._state = RedisHealthState.UNHEALTHY
            logger.critical(
                "redis_health.unhealthy_consecutive_failures_error",
                consecutive_failures=self._consecutive_failures,
                error=error,
            )
            self._record_degraded_mode(True)

    def _initiate_recovery(self):
        """Start recovery with jitter to prevent thundering herd."""
        jitter = random.uniform(1, self.recovery_jitter_max)
        self._recovery_time = time.time() + jitter
        self._state = RedisHealthState.RECOVERING

        logger.info(
            "redis_health.recovery_initiated_jitter",
            jitter=jitter,
        )

    def _complete_recovery_if_ready(self):
        """Complete recovery after jitter delay."""
        if self._recovery_time and time.time() >= self._recovery_time:
            self._state = RedisHealthState.HEALTHY
            self._consecutive_failures = 0
            self._recovery_time = None

            logger.info("redis_health.recovered_resuming_normal_operation")
            self._record_degraded_mode(False)

    def _get_redis_client(self):
        """Get Redis client from Django cache."""
        try:
            from django.core.cache import caches

            # caches is a CacheHandler - use [] indexing, not .get()
            cache = caches["default"]
            if cache is None:
                return None

            # Try to get the underlying client
            if hasattr(cache, "client"):
                client = cache.client
                if hasattr(client, "get_client"):
                    return client.get_client()

            # For django-redis
            if hasattr(cache, "_cache"):
                return cache._cache.get_client()

            return None
        except Exception as e:
            logger.debug(
                "redis_health.get_redis_client",
                error=e,
            )
            return None

    def _record_degraded_mode(self, is_degraded: bool):
        """Update Prometheus metrics."""
        try:
            _, degraded_mode, failover_total = _get_metrics()
            if degraded_mode:
                degraded_mode.set(1 if is_degraded else 0)
            if is_degraded and failover_total:
                failover_total.inc()
        except Exception:
            pass

    def reset(self):
        """Reset health checker state (for testing)."""
        with self._lock:
            self._state = RedisHealthState.HEALTHY
            self._consecutive_failures = 0
            self._last_check_time = 0
            self._recovery_time = None
            self._health_ping_client = None
