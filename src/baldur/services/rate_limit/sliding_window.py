"""
Unified sliding-window rate limiter — L1 (in-process memory).

Thread-safe, framework-free sliding-window counter. Both the Django
hybrid middleware (L1 emergency fallback) and the framework-free
middleware consume this single implementation.

Window-coupling invariant: ``_cleanup_expired`` prunes all stored keys
using the window supplied at cleanup time. Callers that share a
``SlidingWindowLimiter`` instance must use a consistent
``window_seconds`` across calls. Separate singletons (D7) satisfy this
structurally; the warn-only mismatch detector (D2) catches accidents.

Consolidated from ``api/django/rate_limit/local_limiter.LocalMemoryRateLimiter``
and ``api/middleware/rate_limit._SlidingWindowLimiter``
per ``431``.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict
from dataclasses import dataclass

__all__ = ["RateLimitState", "SlidingWindowLimiter"]

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RateLimitState:
    """Snapshot of a client's rate-limit state at decision time."""

    limit: int
    remaining: int
    reset_at: int
    allowed: bool


class SlidingWindowLimiter:
    """Thread-safe in-process sliding-window rate limiter.

    All decision methods (``check``, ``peek``, ``get_client_status``)
    take ``max_requests`` and ``window_seconds`` per-call so settings
    changes take effect immediately without singleton recreation.
    """

    def __init__(self, cleanup_interval: float = 60.0) -> None:
        self._requests: dict[str, list[float]] = defaultdict(list)
        self._lock = threading.Lock()
        self._last_cleanup: float = time.time()
        self._cleanup_interval: float = cleanup_interval
        self._last_seen_window: int | None = None

    def check(
        self,
        key: str,
        max_requests: int,
        window_seconds: int,
    ) -> RateLimitState:
        """Record a hit and return the rate-limit decision."""
        now = time.time()
        window_start = now - window_seconds
        reset_at = int(now + window_seconds)

        with self._lock:
            self._warn_on_window_mismatch(window_seconds)

            if now - self._last_cleanup > self._cleanup_interval:
                self._cleanup_expired(now, window_seconds)
                self._last_cleanup = now

            timestamps = [ts for ts in self._requests[key] if ts > window_start]
            current_count = len(timestamps)

            if current_count >= max_requests:
                self._requests[key] = timestamps
                return RateLimitState(
                    limit=max_requests,
                    remaining=0,
                    reset_at=reset_at,
                    allowed=False,
                )

            timestamps.append(now)
            self._requests[key] = timestamps
            return RateLimitState(
                limit=max_requests,
                remaining=max_requests - current_count - 1,
                reset_at=reset_at,
                allowed=True,
            )

    def peek(
        self,
        key: str,
        max_requests: int,
        window_seconds: int,
    ) -> RateLimitState:
        """Read the latest state without recording a new hit."""
        now = time.time()
        window_start = now - window_seconds
        with self._lock:
            timestamps = [ts for ts in self._requests.get(key, []) if ts > window_start]
        current_count = len(timestamps)
        return RateLimitState(
            limit=max_requests,
            remaining=max(0, max_requests - current_count),
            reset_at=int(now + window_seconds),
            allowed=current_count < max_requests,
        )

    def get_client_status(
        self,
        key: str,
        max_requests: int,
        window_seconds: int,
    ) -> dict:
        """Return xtest-compatible dict for a specific client."""
        state = self.peek(key, max_requests, window_seconds)
        return {
            "client_key": key,
            "current_count": state.limit - state.remaining,
            "limit": state.limit,
            "remaining": state.remaining,
            "reset_at": state.reset_at,
            "blocked": not state.allowed,
            "window_seconds": window_seconds,
        }

    def get_all_clients(self) -> list[str]:
        """Return all currently tracked client keys."""
        with self._lock:
            return list(self._requests.keys())

    def reset_client(self, key: str) -> bool:
        """Reset rate-limit state for a specific client."""
        with self._lock:
            if key in self._requests:
                del self._requests[key]
                return True
            return False

    def reset(self) -> None:
        """Clear all state and the last-seen window tracker."""
        with self._lock:
            self._requests.clear()
            self._last_seen_window = None

    def _warn_on_window_mismatch(self, window_seconds: int) -> None:
        """D2: Warn if a different window is used on the same instance."""
        if self._last_seen_window is None:
            self._last_seen_window = window_seconds
        elif self._last_seen_window != window_seconds:
            logger.warning(
                "rate_limit.window_mismatch",
                extra={
                    "previous_window": self._last_seen_window,
                    "current_window": window_seconds,
                },
            )
            self._last_seen_window = window_seconds

    def _cleanup_expired(self, now: float, window_seconds: int) -> None:
        """Remove entries outside the current window to bound memory."""
        window_start = now - window_seconds
        expired = []
        for key, timestamps in self._requests.items():
            kept = [ts for ts in timestamps if ts > window_start]
            if kept:
                self._requests[key] = kept
            else:
                expired.append(key)
        for key in expired:
            del self._requests[key]
