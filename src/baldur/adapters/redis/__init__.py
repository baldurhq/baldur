# verified-by: test_wal_survives_memory_clear
"""
Redis-based Repository Adapters.

Provides Redis implementations for:
- CircuitBreakerStateRepository
- DLQRepository (FailedOperationRepository)

Uses ResilientStorageBackend for zero data loss guarantees.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import structlog

from baldur.adapters.redis.circuit_breaker import (
    RedisCircuitBreakerStateRepository,
)
from baldur.adapters.redis.dlq import RedisDLQRepository
from baldur.adapters.redis.event_journal import RedisEventJournalRepository

logger = structlog.get_logger()

# ---------------------------------------------------------------------------
# Redis client singleton with TTL-based negative caching
# Not using make_singleton_factory: nullable return + TTL negative caching
# + 4-strategy fallback require domain-specific logic (see 448 D10).
#
# 450 Phase 4: state moved into a runtime-scoped ``_RedisClientState`` so
# resetting the active ``BaldurRuntime`` (or swapping it for a test-scoped
# instance) drops the cached client + negative-cache flags atomically.
# All callers — including conftest seed/reseed and the unit tests — read and
# write through :func:`_redis_state` directly; no module-level mirror remains.
# ---------------------------------------------------------------------------
_REDIS_RETRY_INTERVAL: float = 30.0
_redis_client_lock = threading.Lock()


class _RedisClientState:
    """Mutable Redis-client state owned by the active ``BaldurRuntime``."""

    __slots__ = ("client", "unavailable", "fail_time")

    def __init__(self) -> None:
        self.client: Any | None = None
        self.unavailable: bool = False
        self.fail_time: float = 0.0


def _redis_state() -> _RedisClientState:
    from baldur.runtime import get_runtime

    return get_runtime().get_singleton("redis_client_state", _RedisClientState)


def get_redis_client() -> Any | None:
    """
    Return a shared Redis client, or ``None`` when none can be acquired.

    TTL-based negative caching: on a failed acquisition, retries are suppressed
    for ``_REDIS_RETRY_INTERVAL`` seconds so that a Redis outage does not block
    every call on the ~8s TCP connect timeout.

    Acquisition strategies are tried in order:
    1. ResilientStorageBackend
    2. django_redis cache
    3. Django settings ``BALDUR_REDIS_URL``
    4. Environment variables: ``BALDUR_REDIS_URL`` first, then a bare
       ``REDIS_URL`` as a last-resort backward-compat fallback.

    Returns:
        The Redis client, or ``None``.
    """
    state = _redis_state()

    # Fast path: already connected
    if state.client is not None:
        return state.client

    with _redis_client_lock:
        # Double-check after acquiring lock
        if state.client is not None:
            return state.client

        # Negative cache: suppress retries for _REDIS_RETRY_INTERVAL
        if state.unavailable:
            elapsed = time.monotonic() - state.fail_time
            if elapsed < _REDIS_RETRY_INTERVAL:
                return None
            # TTL expired — allow retry
            logger.info(
                "redis.retry_after_unavailable",
                elapsed_seconds=round(elapsed, 1),
            )
            state.unavailable = False

        client = _try_acquire_redis_client()
        if client is not None:
            state.client = client
            return client

        # All strategies failed — activate negative cache
        state.unavailable = True
        state.fail_time = time.monotonic()
        logger.debug("redis.no_redis_client_available")
    return None


def _try_acquire_redis_client() -> Any | None:
    """Try all Redis acquisition strategies. Returns client or None."""
    # Strategy 1: ResilientStorageBackend
    try:
        from baldur.adapters.resilient.backend import ResilientStorageBackend

        backend = ResilientStorageBackend()
        # `get_redis_client` is duck-typed — ResilientStorageBackend may
        # expose it in PRO impls, OSS shim doesn't. Fall through on miss.
        get_client = getattr(backend, "get_redis_client", None)
        if get_client is not None:
            client = get_client()
            if client:
                return client
    except (ImportError, Exception):
        pass

    # Strategy 2: django_redis
    try:
        from django_redis import get_redis_connection

        return get_redis_connection("default")
    except (ImportError, Exception):
        pass

    # Strategy 3: Django settings BALDUR_REDIS_URL
    try:
        from django.conf import settings

        from baldur.adapters.redis.connection_factory import (
            get_redis_connection_factory,
        )

        redis_url = getattr(settings, "BALDUR_REDIS_URL", None)
        if redis_url:
            return get_redis_connection_factory().create(redis_url)
    except (ImportError, Exception):
        pass

    # Strategy 4: environment-variable fallback
    client = _acquire_from_env()
    if client is not None:
        return client

    return None


def _acquire_from_env() -> Any | None:
    """Resolve a Redis client from environment variables (Strategy 4).

    Prefers the documented canonical ``BALDUR_REDIS_URL`` over the bare,
    non-prefixed ``REDIS_URL``. The bare variable is retained only as a
    lower-priority backward-compat fallback, reached when ``BALDUR_REDIS_URL``
    is unset.

    Returns the connected client, or ``None`` when neither variable is set or
    the connection factory raises.
    """
    try:
        import os

        from baldur.adapters.redis.connection_factory import (
            get_redis_connection_factory,
        )

        baldur_url = os.environ.get("BALDUR_REDIS_URL")
        redis_url = baldur_url or os.environ.get("REDIS_URL")
        if redis_url:
            # Source name only — a Redis URL can embed credentials.
            logger.debug(
                "redis.client_url_resolved",
                source="BALDUR_REDIS_URL" if baldur_url else "REDIS_URL",
            )
            return get_redis_connection_factory().create(redis_url)
    except (ImportError, Exception):
        pass

    return None


def reset_redis_client() -> None:
    """Reset cached Redis client and negative cache (for testing)."""
    state = _redis_state()
    with _redis_client_lock:
        state.client = None
        state.unavailable = False
        state.fail_time = 0.0


__all__ = [
    "RedisCircuitBreakerStateRepository",
    "RedisDLQRepository",
    "RedisEventJournalRepository",
    "get_redis_client",
    "reset_redis_client",
    "RedisConnectionFactory",
    "get_redis_connection_factory",
    "reset_redis_connection_factory",
    "RedisConfigHistoryStore",
    "RedisCanaryRolloutStore",
    "RedisChaosExperimentStore",
    "RedisCrossClusterStore",
]


def __getattr__(name: str) -> Any:
    if name == "RedisConfigHistoryStore":
        from baldur.adapters.redis.config_history import RedisConfigHistoryStore

        return RedisConfigHistoryStore
    if name == "RedisCanaryRolloutStore":
        from baldur.adapters.redis.canary_rollout import RedisCanaryRolloutStore

        return RedisCanaryRolloutStore
    if name == "RedisChaosExperimentStore":
        from baldur.adapters.redis.chaos_experiment import (
            RedisChaosExperimentStore,
        )

        return RedisChaosExperimentStore
    if name == "RedisCrossClusterStore":
        from baldur.adapters.redis.cross_cluster import RedisCrossClusterStore

        return RedisCrossClusterStore
    if name in (
        "RedisConnectionFactory",
        "get_redis_connection_factory",
        "reset_redis_connection_factory",
    ):
        from baldur.adapters.redis.connection_factory import (
            RedisConnectionFactory,
            get_redis_connection_factory,
            reset_redis_connection_factory,
        )

        _exports = {
            "RedisConnectionFactory": RedisConnectionFactory,
            "get_redis_connection_factory": get_redis_connection_factory,
            "reset_redis_connection_factory": reset_redis_connection_factory,
        }
        return _exports[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
