"""
Jitter Utilities for Thundering Herd Prevention.

Provides random delay mechanisms to prevent all instances from
hitting the database simultaneously during startup.

Note: This module was moved from metrics/jitter.py since jitter
utilities are general-purpose and not metrics-specific.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Callable
from functools import wraps
from typing import ParamSpec, TypeVar

import structlog

logger = structlog.get_logger()

P = ParamSpec("P")
R = TypeVar("R")


def with_jitter(
    max_delay_seconds: float | None = None,
    min_delay_seconds: float | None = None,
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """
    Decorator that adds a random delay to a sync function.

    Spreads the DB queries of instances that start simultaneously in a
    distributed environment over time, preventing a thundering herd.

    Args:
        max_delay_seconds: Maximum delay (seconds). Loaded from Settings
            when None.
        min_delay_seconds: Minimum delay (seconds). Loaded from Settings
            when None.

    Example:
        >>> @with_jitter(max_delay_seconds=30.0)
        ... def sync_metrics():
        ...     # Runs after a random delay of 0-30 seconds
        ...     return do_sync()

    Recommended settings per environment:
        - Single server: 0s (disabled)
        - K8s 10 pods: 30s
        - K8s 100+ pods: 60s
    """
    # Load defaults from Settings
    if max_delay_seconds is None or min_delay_seconds is None:
        from baldur.settings.jitter import get_jitter_settings

        settings = get_jitter_settings()
        if max_delay_seconds is None:
            max_delay_seconds = settings.max_delay_seconds
        if min_delay_seconds is None:
            min_delay_seconds = settings.min_delay_seconds

    def decorator(func: Callable[P, R]) -> Callable[P, R]:
        @wraps(func)
        def sync_wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            jitter = random.uniform(min_delay_seconds, max_delay_seconds)
            logger.debug(
                "jitter.sleeping_before",
                jitter=jitter,
                func=func.__name__,
            )
            time.sleep(jitter)
            return func(*args, **kwargs)

        @wraps(func)
        async def async_wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            jitter = random.uniform(min_delay_seconds, max_delay_seconds)
            logger.debug(
                "jitter.sleeping_before",
                jitter=jitter,
                func=func.__name__,
            )
            await asyncio.sleep(jitter)
            return await func(*args, **kwargs)  # type: ignore[misc,no-any-return]

        if asyncio.iscoroutinefunction(func):
            return async_wrapper  # type: ignore
        return sync_wrapper

    return decorator


def calculate_jitter(
    max_delay_seconds: float | None = None,
    min_delay_seconds: float | None = None,
) -> float:
    """
    Compute a jitter delay.

    Call directly when the decorator cannot be used.

    Args:
        max_delay_seconds: Maximum delay (seconds). Loaded from Settings
            when None.
        min_delay_seconds: Minimum delay (seconds). Loaded from Settings
            when None.

    Returns:
        Computed delay (seconds)

    Example:
        >>> delay = calculate_jitter(max_delay_seconds=30.0)
        >>> time.sleep(delay)
        >>> do_sync()
    """
    if max_delay_seconds is None or min_delay_seconds is None:
        from baldur.settings.jitter import get_jitter_settings

        settings = get_jitter_settings()
        if max_delay_seconds is None:
            max_delay_seconds = settings.max_delay_seconds
        if min_delay_seconds is None:
            min_delay_seconds = settings.min_delay_seconds
    return random.uniform(min_delay_seconds, max_delay_seconds)


def sleep_with_jitter(
    max_delay_seconds: float | None = None,
    min_delay_seconds: float | None = None,
) -> float:
    """
    Sleep synchronously with jitter applied.

    Args:
        max_delay_seconds: Maximum delay (seconds). Loaded from Settings
            when None.
        min_delay_seconds: Minimum delay (seconds). Loaded from Settings
            when None.

    Returns:
        Time actually waited (seconds)

    Example:
        >>> waited = sleep_with_jitter(max_delay_seconds=30.0)
        >>> print(f"Waited {waited:.2f} seconds")
    """
    delay = calculate_jitter(max_delay_seconds, min_delay_seconds)
    time.sleep(delay)
    return delay


async def async_sleep_with_jitter(
    max_delay_seconds: float | None = None,
    min_delay_seconds: float | None = None,
) -> float:
    """
    Sleep asynchronously with jitter applied.

    Args:
        max_delay_seconds: Maximum delay (seconds). Loaded from Settings
            when None.
        min_delay_seconds: Minimum delay (seconds). Loaded from Settings
            when None.

    Returns:
        Time actually waited (seconds)

    Example:
        >>> waited = await async_sleep_with_jitter(max_delay_seconds=30.0)
        >>> print(f"Waited {waited:.2f} seconds")
    """
    delay = calculate_jitter(max_delay_seconds, min_delay_seconds)
    await asyncio.sleep(delay)
    return delay


class JitterConfig:
    """
    Jitter configuration class.

    Configures jitter from environment variables or direct settings.
    """

    def __init__(
        self,
        enabled: bool = True,
        max_delay_seconds: float = 60.0,
        min_delay_seconds: float = 0.0,
    ):
        """
        Initialize JitterConfig.

        Args:
            enabled: Whether jitter is enabled
            max_delay_seconds: Maximum delay
            min_delay_seconds: Minimum delay
        """
        self.enabled = enabled
        self.max_delay_seconds = max_delay_seconds
        self.min_delay_seconds = min_delay_seconds

    @classmethod
    def from_settings(cls, settings=None, **overrides) -> JitterConfig:
        """
        Create an instance from Settings.

        Args:
            settings: JitterSettings instance (loaded automatically when None)
            **overrides: Per-field overrides

        Returns:
            JitterConfig: Settings-based instance
        """
        from baldur.settings.jitter import get_jitter_settings

        s = settings or get_jitter_settings()
        return cls(
            enabled=overrides.get("enabled", s.enabled),
            max_delay_seconds=overrides.get("max_delay_seconds", s.max_delay_seconds),
            min_delay_seconds=overrides.get("min_delay_seconds", s.min_delay_seconds),
        )

    def get_delay(self) -> float:
        """Return the jitter delay (0 when disabled)."""
        if not self.enabled:
            return 0.0
        return calculate_jitter(self.max_delay_seconds, self.min_delay_seconds)

    def sleep(self) -> float:
        """Sleep with jitter applied."""
        delay = self.get_delay()
        if delay > 0:
            time.sleep(delay)
        return delay

    async def async_sleep(self) -> float:
        """Sleep asynchronously with jitter applied."""
        delay = self.get_delay()
        if delay > 0:
            await asyncio.sleep(delay)
        return delay


__all__ = [
    "with_jitter",
    "calculate_jitter",
    "sleep_with_jitter",
    "async_sleep_with_jitter",
    "JitterConfig",
]
