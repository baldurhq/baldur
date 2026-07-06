"""
Rate Limit Tracker — Hybrid L1 (Memory) + L2 (Redis)

Thread-safe tracker for rate limit events to detect rate limit cascades
and self-DDoS situations.

L1 (MemoryRateLimitTracker) is always available.
L2 (RedisRateLimitBackend) is lazily initialized when
BALDUR_CB_RATE_LIMIT_DISTRIBUTED=True, following the
ResilientStorageBackend._ensure_redis() pattern (30s retry cooldown).

Read path: L2-prefer, L1-fallback (cluster-wide view when healthy).
Write path: L1 always, L2 fire-and-forget.
"""
# D5: write path is L1-always + L2 fire-and-forget.

from __future__ import annotations

import threading
import time
from collections import defaultdict
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from .rate_limit_lua import RedisRateLimitBackend

logger = structlog.get_logger()

__all__ = [
    "MemoryRateLimitTracker",
    "RateLimitTracker",
    "get_rate_limit_tracker",
    "reset_rate_limit_tracker",
]

_REDIS_PROBE_INTERVAL = 30.0

# L2 retention guards (internal, not operator dials): the shared Redis window
# never retains less than this floor, and the key TTL pads the retention window
# by this many seconds so entries survive slightly past their window.
_MIN_L2_RETENTION_SECONDS = 120
_L2_TTL_PADDING_SECONDS = 60


class MemoryRateLimitTracker:
    """Thread-safe in-memory rate limit event tracker (L1)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._rate_limit_events: dict[str, list[float]] = defaultdict(list)
        self._request_events: dict[str, list[float]] = defaultdict(list)
        self._backoff_levels: dict[str, int] = defaultdict(int)

    def record_rate_limit(self, service_name: str) -> None:
        """Record a 429 rate limit response."""
        with self._lock:
            self._rate_limit_events[service_name].append(time.time())

    def record_request(self, service_name: str) -> None:
        """Record a request attempt."""
        with self._lock:
            self._request_events[service_name].append(time.time())

    def get_rate_limit_count(self, service_name: str, window_seconds: int) -> int:
        """Get the number of rate limits in the time window."""
        cutoff = time.time() - window_seconds
        with self._lock:
            self._rate_limit_events[service_name] = [
                t for t in self._rate_limit_events[service_name] if t > cutoff
            ]
            return len(self._rate_limit_events[service_name])

    def get_request_count(self, service_name: str, window_seconds: int) -> int:
        """Get the number of requests in the time window."""
        cutoff = time.time() - window_seconds
        with self._lock:
            self._request_events[service_name] = [
                t for t in self._request_events[service_name] if t > cutoff
            ]
            return len(self._request_events[service_name])

    def get_backoff_level(self, service_name: str) -> int:
        """Get current backoff level for a service."""
        with self._lock:
            return self._backoff_levels[service_name]

    def increment_backoff(self, service_name: str) -> int:
        """Increment and return the new backoff level."""
        with self._lock:
            self._backoff_levels[service_name] += 1
            return self._backoff_levels[service_name]

    def reset_backoff(self, service_name: str) -> None:
        """Reset backoff level to zero."""
        with self._lock:
            self._backoff_levels[service_name] = 0

    def clear_service(self, service_name: str) -> None:
        """Clear all tracking data for a service."""
        with self._lock:
            self._rate_limit_events[service_name].clear()
            self._request_events[service_name].clear()
            self._backoff_levels[service_name] = 0


class RateLimitTracker:
    """Hybrid L1 (memory) + L2 (Redis) rate limit tracker."""

    def __init__(self):
        self._memory = MemoryRateLimitTracker()
        self._redis: RedisRateLimitBackend | None = None
        self._redis_initialized = False
        self._next_redis_probe = 0.0
        self._lock = threading.Lock()

    def _ensure_redis(self) -> bool:
        """Lazy Redis init with 30s retry cooldown."""
        if self._redis_initialized:
            return True
        if time.monotonic() < self._next_redis_probe:
            return False

        with self._lock:
            if self._redis_initialized:
                return True
            if time.monotonic() < self._next_redis_probe:
                return False

            try:
                from baldur.settings.circuit_breaker import get_circuit_breaker_settings

                settings = get_circuit_breaker_settings()
                if not settings.rate_limit_distributed:
                    return False

                from baldur.adapters.cache import RedisCacheAdapter
                from baldur.settings.redis import get_redis_settings

                redis_adapter = RedisCacheAdapter(
                    url=None,
                    key_prefix="",
                    socket_connect_timeout=get_redis_settings().probe_connect_timeout,
                )
                redis_adapter._redis.ping()

                max_window = max(
                    settings.rate_limit_cascade_window_seconds,
                    settings.self_ddos_window_seconds,
                    _MIN_L2_RETENTION_SECONDS,
                )

                from .rate_limit_lua import RedisRateLimitBackend

                self._redis = RedisRateLimitBackend(
                    redis_adapter._redis,
                    retention_seconds=max_window,
                    ttl_seconds=max_window + _L2_TTL_PADDING_SECONDS,
                )
                self._redis_initialized = True
                logger.info("rate_limit_tracker.redis_connected")
                return True
            except Exception as e:
                logger.warning(
                    "rate_limit_tracker.redis_probe_failed",
                    error=str(e),
                )
                self._next_redis_probe = time.monotonic() + _REDIS_PROBE_INTERVAL
                return False

    # =========================================================================
    # Write: L1 always + L2 fire-and-forget
    # =========================================================================

    def record_rate_limit(self, service_name: str) -> None:
        """Record a 429 rate limit response."""
        self._memory.record_rate_limit(service_name)
        if self._ensure_redis():
            assert self._redis is not None
            try:
                self._redis.record_rate_limit(service_name)
            except Exception:
                pass

    def record_request(self, service_name: str) -> None:
        """Record a request attempt."""
        self._memory.record_request(service_name)
        if self._ensure_redis():
            assert self._redis is not None
            try:
                self._redis.record_request(service_name)
            except Exception:
                pass

    # =========================================================================
    # Read: L2-prefer, L1-fallback
    # =========================================================================

    def get_rate_limit_count(self, service_name: str, window_seconds: int) -> int:
        """Get the number of rate limits in the time window."""
        if self._ensure_redis():
            assert self._redis is not None
            try:
                return self._redis.get_rate_limit_count(service_name, window_seconds)
            except Exception:
                pass
        return self._memory.get_rate_limit_count(service_name, window_seconds)

    def get_request_count(self, service_name: str, window_seconds: int) -> int:
        """Get the number of requests in the time window."""
        if self._ensure_redis():
            assert self._redis is not None
            try:
                return self._redis.get_request_count(service_name, window_seconds)
            except Exception:
                pass
        return self._memory.get_request_count(service_name, window_seconds)

    def get_backoff_level(self, service_name: str) -> int:
        """Get current backoff level for a service."""
        if self._ensure_redis():
            assert self._redis is not None
            try:
                return self._redis.get_backoff_level(service_name)
            except Exception:
                pass
        return self._memory.get_backoff_level(service_name)

    def increment_backoff(self, service_name: str) -> int:
        """Increment and return the new backoff level."""
        level = self._memory.increment_backoff(service_name)
        if self._ensure_redis():
            assert self._redis is not None
            try:
                level = self._redis.increment_backoff(service_name)
            except Exception:
                pass
        return level

    def reset_backoff(self, service_name: str) -> None:
        """Reset backoff level to zero."""
        self._memory.reset_backoff(service_name)
        if self._ensure_redis():
            assert self._redis is not None
            try:
                self._redis.reset_backoff(service_name)
            except Exception:
                pass

    def clear_service(self, service_name: str) -> None:
        """Clear all tracking data for a service."""
        self._memory.clear_service(service_name)
        if self._ensure_redis():
            assert self._redis is not None
            try:
                self._redis.clear_service(service_name)
            except Exception:
                pass


# =============================================================================
# Singleton
# =============================================================================

_rate_limit_tracker: RateLimitTracker | None = None
_rate_limit_tracker_lock = threading.Lock()


def get_rate_limit_tracker() -> RateLimitTracker:
    """Get the singleton rate limit tracker instance."""
    global _rate_limit_tracker
    if _rate_limit_tracker is None:
        with _rate_limit_tracker_lock:
            if _rate_limit_tracker is None:
                _rate_limit_tracker = RateLimitTracker()
    return _rate_limit_tracker


def reset_rate_limit_tracker() -> None:
    """Reset the singleton rate limit tracker instance."""
    global _rate_limit_tracker
    with _rate_limit_tracker_lock:
        _rate_limit_tracker = None
