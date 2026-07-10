"""
Unified sliding-window rate limiter — L1 (in-process memory).

Thread-safe, framework-free sliding-window counter. Both the Django
hybrid middleware (L1 emergency fallback) and the framework-free
middleware consume this single implementation.

Window-coupling invariant: the periodic stale-key sweep prunes all
stored keys using the window supplied at check time. Callers that share
a ``SlidingWindowLimiter`` instance must use a consistent
``window_seconds`` across calls. Separate singletons satisfy this
structurally; the warn-only mismatch detector catches accidents.

Delegates the window arithmetic to the shared
``SlidingWindowCounter`` primitive; this wrapper keeps the public
``RateLimitState`` decision shape and the window-mismatch warning.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

from baldur.core.rate_limiting import SlidingWindowCounter

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
        self._cleanup_interval = cleanup_interval
        self._counter = SlidingWindowCounter(cleanup_interval=cleanup_interval)
        self._lock = threading.Lock()
        self._last_seen_window: int | None = None

    def check(
        self,
        key: str,
        max_requests: int,
        window_seconds: int,
    ) -> RateLimitState:
        """Record a hit and return the rate-limit decision."""
        now = time.time()
        reset_at = int(now + window_seconds)

        with self._lock:
            self._warn_on_window_mismatch(window_seconds)

        allowed, current_count = self._counter.try_acquire(
            key, max_requests, window_seconds
        )
        remaining = max_requests - current_count if allowed else 0
        return RateLimitState(
            limit=max_requests,
            remaining=remaining,
            reset_at=reset_at,
            allowed=allowed,
        )

    def peek(
        self,
        key: str,
        max_requests: int,
        window_seconds: int,
    ) -> RateLimitState:
        """Read the latest state without recording a new hit."""
        now = time.time()
        current_count = self._counter.count(key, window_seconds)
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
        return self._counter.keys()

    def reset_client(self, key: str) -> bool:
        """Reset rate-limit state for a specific client."""
        return self._counter.reset(key)

    def reset(self) -> None:
        """Clear all state and the last-seen window tracker."""
        self._counter.reset_all()
        with self._lock:
            self._last_seen_window = None

    def _warn_on_window_mismatch(self, window_seconds: int) -> None:
        """Warn if a different window is used on the same instance."""
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
