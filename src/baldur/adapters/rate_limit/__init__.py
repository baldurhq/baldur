"""
Rate Limit Storage Adapters

Concrete implementations of RateLimitStorageInterface for different backends.

Available adapters:
- RedisRateLimitStorage: Fastest, requires Redis. Shared across processes.
- DatabaseRateLimitStorage: Any database. Opt-in only — auto-detection never
  selects it, and it needs a repository factory the caller supplies.
- InMemoryRateLimitStorage: Single process only, for testing

Auto-detection resolves Redis when it is configured and the in-memory store
otherwise, so a deployment without Redis coordinates per process rather than
across the fleet.

Usage:
    from baldur.adapters.rate_limit import (
        get_rate_limit_storage,
        RedisRateLimitStorage,
        DatabaseRateLimitStorage,
        InMemoryRateLimitStorage,
    )

    # Auto-detect best available backend
    storage = get_rate_limit_storage()

    # Or explicitly choose
    storage = RedisRateLimitStorage(redis_client)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from baldur.adapters.rate_limit.database_adapter import DatabaseRateLimitStorage
from baldur.adapters.rate_limit.memory_adapter import InMemoryRateLimitStorage
from baldur.adapters.rate_limit.redis_adapter import RedisRateLimitStorage

if TYPE_CHECKING:
    from baldur.interfaces.rate_limit_storage import RateLimitStorageInterface

logger = structlog.get_logger()

__all__ = [
    "RedisRateLimitStorage",
    "DatabaseRateLimitStorage",
    "InMemoryRateLimitStorage",
    "get_rate_limit_storage",
]


def get_rate_limit_storage(
    backend: str | None = None,
) -> RateLimitStorageInterface:
    """Get rate limit storage via ProviderRegistry.

    When backend is None (default), attempts providers in priority order:
    Redis -> Database -> Memory. This preserves the auto-detection behavior
    of the previous custom factory.

    The Redis provider is skipped outright when nobody named a Redis outside
    production: constructing it would dial the shipped default address, and
    that connect happens on the first protected call, inside the caller's own
    timed section. An explicit ``backend="redis"`` is unaffected — an
    explicit ask wins.

    Args:
        backend: Explicit backend name ('redis', 'database', 'memory').
                 None triggers auto-detection with fallback.

    Returns:
        RateLimitStorageInterface implementation
    """
    from baldur.factory import ProviderRegistry

    reg = ProviderRegistry.rate_limit_storage

    if backend is not None:
        return reg.get(backend)

    # Auto-detect: try providers in priority order (Redis -> Database -> Memory)
    for name in ("redis", "database", "memory"):
        if not reg.has_provider(name):
            continue
        if name == "redis" and _redis_absence_is_expected():
            _announce_unconfigured_redis_skip()
            continue
        try:
            instance = reg.get(name)
            if hasattr(instance, "is_available") and not instance.is_available():
                # Clear cached instance so next attempt starts fresh
                reg.invalidate_instance(name)
                continue
            logger.info(
                "rate_limit_storage.auto_detected",
                backend=name,
            )
            return instance
        except Exception:
            # Clear failed instance from cache
            reg.invalidate_instance(name)
            logger.debug(
                "rate_limit_storage.auto_detect_skipped",
                backend=name,
            )

    # Final fallback: always-available memory backend
    logger.warning("rate_limit_storage.falling_back_memory_storage")
    return reg.get("memory")


def _redis_absence_is_expected() -> bool:
    """Would the Redis provider be dialing an address nobody named?

    True only when no channel expressed Redis intent AND this is not
    production — the same predicate the resilient storage backend gates its
    own quiet posture on. An internal failure resolves to False, which puts
    the loop back on its current probe-then-fall-back path: this gate can
    cost the latency fix, never safety.
    """
    from baldur.settings.redis import redis_absence_is_expected

    return redis_absence_is_expected()


def _announce_unconfigured_redis_skip() -> None:
    """Record that auto-detection declined to construct the Redis provider.

    Keeps the shipped fallback gauge honest. Today the loop constructs the
    provider and ``is_available()``'s failure branch is what sets the gauge,
    so a zero-config process reports fallback-active while it coordinates per
    process. Skipping the construction without this write would leave the
    gauge at its default while the coordinator is still per-process — a
    silent under-report of exactly the state the gauge names.

    ``record_ratelimit_redis_unavailable()`` is deliberately NOT called:
    nothing was attempted, so no attempt failed.
    """
    try:
        from baldur.metrics.drift_metrics import set_ratelimit_fallback_mode
    except ImportError:
        pass
    else:
        set_ratelimit_fallback_mode(True)

    logger.debug(
        "rate_limit_storage.redis_provider_skipped",
        reason="redis_not_configured",
    )
