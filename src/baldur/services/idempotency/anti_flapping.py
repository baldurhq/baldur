"""
Anti-Flapping Window

Sliding window based anti-flapping detection for parameter auto-tuning.

Canonical location: ``baldur.services.idempotency.anti_flapping``
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict
from threading import Lock
from typing import (
    Any,  # noqa: F401  # used in type annotation under `from __future__ import annotations`
)

import structlog

logger = structlog.get_logger()


class AntiFlappingWindow:
    """
    Anti-Flapping window (sliding-window based).

    Detects identical or similar values repeating within a short time.

    Distributed-environment support (v2.4.0):
    - When Redis is available: ZSET-based distributed sliding window
    - When Redis is unavailable: memory-based local window (legacy behavior)

    Reference:
    - Architect Review: "treat adjustment repetitions under 1% as duplicates/loops"
    - Reuses the existing SlidingWindowThrottle pattern
    """

    REDIS_KEY_PREFIX = "baldur:anti_flapping:"

    def __init__(
        self,
        window_seconds: int = 60,
        similarity_threshold: float = 0.01,  # within 1% = similar
        max_similar_changes: int = 3,
        use_redis: bool = True,
    ):
        """
        Initialize AntiFlappingWindow.

        Args:
            window_seconds: Sliding-window size (seconds)
            similarity_threshold: Similarity-decision threshold (0.01 = 1%)
            max_similar_changes: Max similar changes within the window
            use_redis: Whether to use Redis (distributed-environment support)
        """
        self.window_seconds = window_seconds
        self.similarity_threshold = similarity_threshold
        self.max_similar_changes = max_similar_changes
        self._use_redis = use_redis

        # Memory-based local window (fallback)
        # key -> [(timestamp, value), ...]
        self._windows: dict[str, list[tuple[float, float]]] = defaultdict(list)
        self._lock = Lock()

        # Initialize the Redis client
        self._redis_client: Any | None = None
        if use_redis:
            self._init_redis_client()

    def _init_redis_client(self) -> None:
        """Initialize the Redis client."""
        try:
            from baldur.core.state_backend import (
                RedisStateBackend,
                get_state_backend,
            )

            backend = get_state_backend()
            if isinstance(backend, RedisStateBackend):
                self._redis_client = backend._client
                logger.info("anti_flapping_window.redis_mode_enabled_distributed")
            else:
                logger.info("anti_flapping_window.local_memory_fallback")
        except Exception as e:
            from baldur.adapters.resilient.backend import _safe_error_message

            logger.warning(
                "resilient_storage.redis_init_failed",
                _safe_error_message=_safe_error_message(e),
            )

    def check_and_record(
        self,
        key: str,
        new_value: float,
    ) -> tuple[bool, str]:
        """
        Check whether a new value is flapping and record it.

        Args:
            key: Parameter key (e.g., "circuit_breaker:threshold")
            new_value: New value

        Returns:
            (is_flapping, reason)
        """
        if self._redis_client:
            return self._check_and_record_redis(key, new_value)
        return self._check_and_record_memory(key, new_value)

    def _check_and_record_redis(
        self,
        key: str,
        new_value: float,
    ) -> tuple[bool, str]:
        """
        Redis ZSET-based distributed sliding window.

        ZSET usage:
        - score: timestamp
        - member: "timestamp:value" string
        - ZRANGEBYSCORE to query values within the window
        - ZREMRANGEBYSCORE to remove expired entries

        Priority 5.3 implementation
        """
        assert self._redis_client is not None
        redis_key = f"{self.REDIS_KEY_PREFIX}{key}"
        now_ts = time.time()
        window_start = now_ts - self.window_seconds

        try:
            pipe = self._redis_client.pipeline()

            # 1. Remove old entries
            pipe.zremrangebyscore(redis_key, "-inf", window_start)

            # 2. Query all entries within the current window
            pipe.zrangebyscore(redis_key, window_start, "+inf", withscores=True)

            results = pipe.execute()
            entries = results[1]  # [(member, score), ...]

            # 3. Count similar value changes
            similar_count = 0
            for member, _ in entries:
                # member format: "timestamp:value"
                try:
                    if isinstance(member, bytes):
                        member = member.decode("utf-8")
                    _, val_str = member.split(":", 1)
                    val = float(val_str)
                    if self._is_similar(val, new_value):
                        similar_count += 1
                except (ValueError, AttributeError):
                    continue

            # 4. Detect flapping
            if similar_count >= self.max_similar_changes:
                return (
                    True,
                    f"Flapping detected: {similar_count} similar changes in {self.window_seconds}s",
                )

            # 5. Record the current value
            member = f"{now_ts}:{new_value}"
            self._redis_client.zadd(redis_key, {member: now_ts})

            # 6. Set TTL (window * 2 to be safe)
            self._redis_client.expire(redis_key, self.window_seconds * 2)

            return False, ""

        except Exception as e:
            logger.warning(
                "anti_flapping_window.redis_error_fallback_memory",
                error=e,
            )
            return self._check_and_record_memory(key, new_value)

    def _check_and_record_memory(
        self,
        key: str,
        new_value: float,
    ) -> tuple[bool, str]:
        """Memory-based local sliding window (legacy logic)."""
        now_ts = time.time()
        window_start = now_ts - self.window_seconds

        with self._lock:
            # Sliding window: remove old entries
            self._windows[key] = [
                (ts, val) for ts, val in self._windows[key] if ts > window_start
            ]

            # Count similar value changes
            similar_count = 0
            for _ts, val in self._windows[key]:
                if self._is_similar(val, new_value):
                    similar_count += 1

            # Detect flapping
            if similar_count >= self.max_similar_changes:
                return (
                    True,
                    f"Flapping detected: {similar_count} similar changes in {self.window_seconds}s",
                )

            # Record the current value
            self._windows[key].append((now_ts, new_value))

            return False, ""

    def _is_similar(self, val1: float, val2: float) -> bool:
        """Check whether two values are similar (within the threshold)."""
        if val1 == 0 and val2 == 0:
            return True
        if val1 == 0 or val2 == 0:
            return False

        diff_ratio = abs(val1 - val2) / max(abs(val1), abs(val2))
        return diff_ratio <= self.similarity_threshold

    def clear_window(self, key: str) -> bool:
        """
        Clear the window for a specific key (for tests).

        Args:
            key: Parameter key

        Returns:
            Whether it succeeded
        """
        if self._redis_client:
            try:
                redis_key = f"{self.REDIS_KEY_PREFIX}{key}"
                self._redis_client.delete(redis_key)
                return True
            except Exception as e:
                logger.warning(
                    "anti_flapping_window.redis_clear_failed",
                    error=e,
                )

        with self._lock:
            if key in self._windows:
                del self._windows[key]
        return True


# Global Anti-Flapping window
_anti_flapping_window: AntiFlappingWindow | None = None
_anti_flapping_window_lock = threading.Lock()


def get_anti_flapping_window() -> AntiFlappingWindow:
    """Get singleton AntiFlappingWindow."""
    global _anti_flapping_window
    if _anti_flapping_window is None:
        with _anti_flapping_window_lock:
            if _anti_flapping_window is None:
                from baldur.settings.anti_flapping import get_anti_flapping_settings

                settings = get_anti_flapping_settings()
                _anti_flapping_window = AntiFlappingWindow(
                    window_seconds=settings.window_seconds,
                    similarity_threshold=settings.similarity_threshold,
                    max_similar_changes=settings.max_similar_changes,
                )
    return _anti_flapping_window


def reset_anti_flapping_window() -> None:
    """Reset singleton (testing / settings reload)."""
    global _anti_flapping_window
    _anti_flapping_window = None
