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
