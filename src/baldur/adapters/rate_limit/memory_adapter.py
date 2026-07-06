"""
In-Memory Rate Limit Storage Adapter

Thread-safe in-memory rate limit storage for single-process environments.
Primarily for testing and development.

Warning:
    This adapter does NOT share state across processes or servers.
    Use RedisRateLimitStorage or DatabaseRateLimitStorage for production
    multi-server environments.

Features:
    - Zero external dependencies
    - Thread-safe operations
    - Automatic cleanup of expired entries
    - Perfect for testing and development
"""

from __future__ import annotations

import threading
import time

import structlog

from baldur.interfaces.rate_limit_storage import (
    RateLimitState,
    RateLimitStorageInterface,
    RateLimitStorageType,
)

logger = structlog.get_logger()


class InMemoryRateLimitStorage(RateLimitStorageInterface):
    """
    In-memory rate limit storage.

    Thread-safe storage for single-process environments.
    State is NOT shared across processes.

    Example:
        storage = InMemoryRateLimitStorage()
        storage.set_cooldown("payment_api", time.time() + 60)

        # Get shared instance
        storage = InMemoryRateLimitStorage.get_instance()

    Warning:
        For multi-server production, use Redis or Database adapters.
    """

    _instance: InMemoryRateLimitStorage | None = None
    _instance_lock = threading.Lock()

    def __init__(self, cleanup_interval: int | None = None) -> None:
        """Initialize in-memory storage.

        Args:
            cleanup_interval: Storage operations between expired-entry cleanup
                sweeps. When None, resolves from
                ``RateLimitSettings.memory_cleanup_interval_ops``.
        """
        self._data: dict[str, dict] = {}
        self._lock = threading.RLock()
        self._cleanup_counter = 0
        if cleanup_interval is None:
            from baldur.settings.rate_limit import get_rate_limit_settings

            cleanup_interval = get_rate_limit_settings().memory_cleanup_interval_ops
        self._cleanup_interval = cleanup_interval

    @classmethod
    def get_instance(cls) -> InMemoryRateLimitStorage:
        """Get singleton instance for process-wide state sharing."""
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """Reset singleton instance (for testing)."""
        with cls._instance_lock:
            cls._instance = None

    @property
    def storage_type(self) -> RateLimitStorageType:
        return RateLimitStorageType.MEMORY

    def _maybe_cleanup(self) -> None:
        """Periodically cleanup expired entries."""
        self._cleanup_counter += 1
        if self._cleanup_counter >= self._cleanup_interval:
            self._cleanup_counter = 0
            self._cleanup_expired()

    def _cleanup_expired(self) -> None:
        """Remove entries with expired cooldowns and zero counters."""
        now = time.time()
        expired_keys = []

        for key, data in self._data.items():
            cooldown_until = data.get("cooldown_until", 0.0)
            consecutive_429s = data.get("consecutive_429s", 0)
            last_updated = data.get("last_updated", 0.0)

            # Consider expired if:
            # - Cooldown has passed AND counter is zero AND not updated recently
            if (
                cooldown_until < now
                and consecutive_429s == 0
                and now - last_updated > 3600
            ):  # 1 hour
                expired_keys.append(key)

        for key in expired_keys:
            del self._data[key]

        if expired_keys:
            logger.debug(
                "in_memory_rate_limit_storage.cleaned_up_expired_entries",
                expired_keys_count=len(expired_keys),
            )

    def get_state(self, key: str) -> RateLimitState:
        """Get rate limit state from memory."""
        with self._lock:
            data = self._data.get(key, {})

            return RateLimitState(
                key=key,
                cooldown_until=data.get("cooldown_until", 0.0),
                consecutive_429s=data.get("consecutive_429s", 0),
                last_updated=data.get("last_updated", 0.0),
            )

    def set_cooldown(
        self,
        key: str,
        cooldown_until: float,
        ttl: int | None = None,
    ) -> None:
        """Set cooldown in memory."""
        with self._lock:
            now = time.time()

            if key not in self._data:
                self._data[key] = {}

            self._data[key]["cooldown_until"] = cooldown_until
            self._data[key]["last_updated"] = now

            self._maybe_cleanup()

            logger.debug(
                "in_memory_rate_limit_storage.set_cooldown",
                rate_limit_key=key,
                cooldown_until=cooldown_until,
            )

    def increment_consecutive_429s(self, key: str) -> int:
        """Increment 429 counter in memory."""
        with self._lock:
            now = time.time()

            if key not in self._data:
                self._data[key] = {"consecutive_429s": 0}

            self._data[key]["consecutive_429s"] = (
                self._data[key].get("consecutive_429s", 0) + 1
            )
            self._data[key]["last_updated"] = now

            new_value = self._data[key]["consecutive_429s"]

            self._maybe_cleanup()

            logger.debug(
                "in_memory_rate_limit_storage.incremented_counter",
                rate_limit_key=key,
                new_value=new_value,
            )
            return new_value

    def reset_consecutive_429s(self, key: str) -> None:
        """Reset 429 counter in memory."""
        with self._lock:
            if key in self._data:
                self._data[key]["consecutive_429s"] = 0
                self._data[key]["last_updated"] = time.time()

            logger.debug(
                "in_memory_rate_limit_storage.reset_counter",
                rate_limit_key=key,
            )

    def clear(self, key: str) -> None:
        """Clear all rate limit state for a key."""
        with self._lock:
            if key in self._data:
                del self._data[key]

            logger.debug(
                "in_memory_rate_limit_storage.cleared_state",
                rate_limit_key=key,
            )

    def clear_all(self) -> None:
        """Clear all rate limit state (for testing)."""
        with self._lock:
            self._data.clear()
            logger.debug("in_memory_rate_limit_storage.cleared_all_state")
