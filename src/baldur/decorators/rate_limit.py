"""``@rate_limit`` — function-level sliding-window rate limiter.

Wraps ``baldur.services.rate_limit.SlidingWindowLimiter`` so a single
decorator covers both sync and async callables. Module-level limiter
sharing keyed by ``window_seconds`` keeps decorators with the same
window in one efficient instance while isolating different windows
(``_cleanup_expired`` would otherwise corrupt cross-window state).
"""

# Reference: 458 §D2, §D5, §D7, §D8.

from __future__ import annotations

import asyncio
import functools
import logging
import threading
from collections.abc import Callable
from typing import Any, TypeVar

from baldur.core.exceptions import RateLimitExceeded
from baldur.services.rate_limit.sliding_window import SlidingWindowLimiter

__all__ = ["rate_limit"]

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Module-level limiter dict — keyed by window_seconds (D7 limiter-sharing rule).
# Sharing one SlidingWindowLimiter across distinct windows is unsafe because
# _cleanup_expired prunes ALL keys using the current call's window.
_LIMITERS: dict[int, SlidingWindowLimiter] = {}
_LIMITERS_LOCK = threading.Lock()


def _get_limiter(window_seconds: int) -> SlidingWindowLimiter:
    """Return the shared limiter for ``window_seconds``, lazily constructing."""
    limiter = _LIMITERS.get(window_seconds)
    if limiter is not None:
        return limiter
    with _LIMITERS_LOCK:
        limiter = _LIMITERS.get(window_seconds)
        if limiter is None:
            limiter = SlidingWindowLimiter()
            _LIMITERS[window_seconds] = limiter
        return limiter


def _reset_limiters() -> None:
    """Test helper — clear the module-level limiter dict for fixture isolation."""
    with _LIMITERS_LOCK:
        _LIMITERS.clear()


def rate_limit(  # noqa: C901
    *,
    max_requests: int,
    window_seconds: int = 60,
    key_fn: Callable[..., str] | None = None,
    raise_on_limit: bool = True,
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """Function-level sliding-window rate limiter.

    Args:
        max_requests: Maximum requests allowed within ``window_seconds``.
            Required — missing raises ``TypeError`` at decoration time.
        window_seconds: Sliding-window size in seconds. Defaults to 60.
        key_fn: Optional ``(*args, **kwargs) -> str`` callable that derives
            the per-call bucket key. When ``None``, a single shared bucket
            keyed by ``func.__qualname__`` is used.
        raise_on_limit: When ``True`` (default), raise ``RateLimitExceeded``
            on rejection. When ``False``, return ``None`` on rejection (the
            wrapped function's value is returned on allowed paths).

    Returns:
        Decorator that auto-detects sync vs async.

    Raises (at call time):
        RateLimitExceeded: When ``raise_on_limit=True`` and the limiter
            rejects the call.

    Usage::

        @rate_limit(max_requests=10, window_seconds=60)
        def search(q: str) -> list[str]:
            ...

        @rate_limit(max_requests=5, window_seconds=1, key_fn=lambda user_id: f"u:{user_id}")
        async def fetch(user_id: int) -> dict:
            ...
    """
    if not isinstance(max_requests, int) or max_requests <= 0:
        raise TypeError(
            f"@rate_limit requires max_requests: positive int. Got {max_requests!r}."
        )
    if not isinstance(window_seconds, int) or window_seconds <= 0:
        raise TypeError(
            "@rate_limit requires window_seconds: positive int. "
            f"Got {window_seconds!r}."
        )

    def _resolve_key(func: Callable[..., Any], args: tuple, kwargs: dict) -> str:
        if key_fn is None:
            return func.__qualname__
        return key_fn(*args, **kwargs)

    def _check_or_skip(
        func: Callable[..., Any], args: tuple, kwargs: dict
    ) -> tuple[bool, str]:
        """Returns (allowed, key). Raises RateLimitExceeded or returns (False, key)
        depending on raise_on_limit. Returns (True, "") when toggle disabled."""
        from baldur.settings.rate_limit import get_rate_limit_settings

        if not get_rate_limit_settings().decorator_enabled:
            return (True, "")

        limiter = _get_limiter(window_seconds)
        key = _resolve_key(func, args, kwargs)
        state = limiter.check(key, max_requests, window_seconds)
        if state.allowed:
            return (True, key)

        # Rejected — D8 logging before raise / before returning None.
        logger.warning(
            "rate_limit.request_blocked",
            extra={
                "function": func.__qualname__,
                "key": key,
                "window_seconds": window_seconds,
                "max_requests": max_requests,
                "remaining": 0,
            },
        )
        if raise_on_limit:
            raise RateLimitExceeded(
                key=key,
                limit=max_requests,
                window_seconds=window_seconds,
                reset_at=state.reset_at,
            )
        return (False, key)

    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        if asyncio.iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                allowed, _ = _check_or_skip(func, args, kwargs)
                if not allowed:
                    return None
                return await func(*args, **kwargs)

            return async_wrapper  # type: ignore[return-value]

        @functools.wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            allowed, _ = _check_or_skip(func, args, kwargs)
            if not allowed:
                return None
            return func(*args, **kwargs)

        return sync_wrapper  # type: ignore[return-value]

    return decorator
