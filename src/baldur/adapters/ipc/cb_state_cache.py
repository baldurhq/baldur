"""
Local cache for circuit breaker state.

Optimizes sidecar IPC by replacing per-call Redis lookups with a local TTL
cache plus event-driven invalidation.

Features:
- TTL-based expiry (default 5 seconds)
- Immediate invalidation via EventBus state-change events
- Thread-safe implementation

Usage:
    from baldur.adapters.ipc.cb_state_cache import IPCStateCache

    cache = IPCStateCache(ttl_seconds=5.0)

    # Lookup
    cached, hit = cache.get("payment_gateway")
    if hit:
        return cached

    # Store after the real service call
    result = cb_service.should_allow("payment_gateway")
    cache.set("payment_gateway", result)
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import structlog

from baldur.core.ttl_cache import TTLCacheBase

logger = structlog.get_logger()


@dataclass
class IPCCacheEntry:
    """Cache entry."""

    value: Any
    """Cached value."""

    expires_at: float
    """Expiration time (Unix timestamp)."""

    created_at: float = field(default_factory=time.time)
    """Creation time."""


class IPCStateCache(TTLCacheBase[str, Any]):
    """
    Local cache for circuit breaker state (used by the IPC adapter).

    Strategy:
    - TTL-based expiry (default 5 seconds)
    - Immediate invalidation via EventBus state-change events
    - Thread-safe implementation
    """

    DEFAULT_TTL_SECONDS = 5.0
    MAX_ENTRIES = 10000  # Maximum number of cache entries

    def __init__(
        self,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        enable_event_invalidation: bool = True,
    ):
        """
        Initialize the cache.

        Args:
            ttl_seconds: cache TTL in seconds
            enable_event_invalidation: whether EventBus invalidation is enabled
        """
        super().__init__(
            ttl_seconds=ttl_seconds,
            max_size=self.MAX_ENTRIES,
        )
        self._subscribed = False

        if enable_event_invalidation:
            self._register_invalidation_handlers()

    @property
    def _ttl(self) -> float:
        """Backward-compatible TTL accessor."""
        return self._ttl_seconds

    def _register_invalidation_handlers(self) -> None:
        """Subscribe to EventBus events that should invalidate the cache."""
        if self._subscribed:
            return
        try:
            from baldur.services.event_bus import EventType, get_event_bus

            bus = get_event_bus()

            # Subscribe to CB state-change events
            for event_type in [
                EventType.CIRCUIT_BREAKER_OPENED,
                EventType.CIRCUIT_BREAKER_CLOSED,
                EventType.CIRCUIT_BREAKER_HALF_OPENED,
            ]:
                bus.subscribe(event_type, self._on_state_change)

            self._subscribed = True
            logger.debug("ipc_state_cache.handlers_registered")
        except ImportError:
            logger.debug("ipc_state_cache.eventbus_available")
        except Exception as e:
            logger.warning(
                "ipc_state_cache.eventbus_registration_failed",
                error=e,
            )

    def close(self) -> None:
        """Unsubscribe EventBus handlers and release cache resources."""
        if self._subscribed:
            try:
                from baldur.services.event_bus import EventType, get_event_bus

                bus = get_event_bus()
                for event_type in [
                    EventType.CIRCUIT_BREAKER_OPENED,
                    EventType.CIRCUIT_BREAKER_CLOSED,
                    EventType.CIRCUIT_BREAKER_HALF_OPENED,
                ]:
                    bus.unsubscribe(event_type, self._on_state_change)
                self._subscribed = False
            except ImportError:
                pass
            except Exception:
                pass
        self.invalidate_all()

    def _on_state_change(self, event: Any) -> None:
        """Event-driven immediate invalidation."""
        try:
            service_name = event.data.get("service_name")
            if service_name:
                self.invalidate(service_name)
                logger.debug(
                    "ipc_state_cache.invalidated",
                    service_name=service_name,
                    event_type=event.event_type.value,
                )
        except Exception as e:
            logger.warning(
                "ipc_state_cache.event_handler_failed",
                error=e,
            )

    def get(self, service_name: str) -> tuple[Any, bool]:  # type: ignore[override]
        """
        Look up a cached value.

        Args:
            service_name: service name

        Returns:
            (value, hit) tuple
        """
        value = super().get(service_name)
        if value is None:
            return None, False
        return value, True

    def set(
        self, service_name: str, value: Any, ttl_override: float | None = None
    ) -> None:
        """
        Store a value in the cache.

        Args:
            service_name: service name
            value: value to store
            ttl_override: TTL override (seconds)
        """
        super().set(service_name, value, ttl_override=ttl_override)

    def get_or_set(
        self,
        service_name: str,
        factory: Any,
    ) -> Any:
        """
        Look up the cache; if missing, build via the factory and store.

        Concurrent same-service misses run the factory exactly once per
        process (inherited TTLCacheBase.get_or_compute singleflight);
        the other callers share the winner's value. A factory returning
        None is returned but not stored - observably identical to the
        previous behavior, where a stored None was indistinguishable
        from a miss on read.

        Args:
            service_name: service name
            factory: value-producing callable, or the value itself

        Returns:
            cached value or newly produced value
        """
        # TTL stays at the instance default (no ttl_override), matching
        # the previous self.set(service_name, value) call.
        return self.get_or_compute(
            service_name,
            lambda: factory() if callable(factory) else factory,
        )

    def get_stats_dict(self) -> dict[str, Any]:
        """Cache statistics as a dictionary."""
        return {
            "size": self.size,
            "ttl_seconds": self._ttl_seconds,
            "hits": self._stats.hits,
            "misses": self._stats.misses,
            "hit_rate": round(self._stats.hit_rate, 4),
            "invalidations": self._stats.invalidations,
            "expirations": self._stats.expirations,
        }

    def contains(self, service_name: str) -> bool:
        """Return True when the service is cached and not expired."""
        _, hit = self.get(service_name)
        return hit

    def keys(self) -> list[str]:
        """List the cached service names."""
        with self._lock:
            now = time.time()
            return [key for key, entry in self._cache.items() if entry.expires_at > now]


# =============================================================================
# Batch cache helper
# =============================================================================


class CBStateBatchCache:
    """
    Wrapper around the CB state cache that supports batch lookups.

    Allows querying or storing the state of several services at once.
    """

    def __init__(self, cache: IPCStateCache | None = None):
        """
        Initialize the batch cache.

        Args:
            cache: existing cache instance (creates a new one when None)
        """
        self._cache = cache or IPCStateCache()

    def get_batch(
        self,
        service_names: list[str],
    ) -> tuple[dict[str, Any], list[str]]:
        """
        Batch cache lookup.

        Args:
            service_names: list of service names

        Returns:
            (cache results, list of misses) tuple
        """
        results: dict[str, Any] = {}
        misses: list[str] = []

        for name in service_names:
            value, hit = self._cache.get(name)
            if hit:
                results[name] = value
            else:
                misses.append(name)

        return results, misses

    def set_batch(self, results: dict[str, Any]) -> None:
        """
        Batch cache store.

        Args:
            results: {service_name: value} dictionary
        """
        for name, value in results.items():
            self._cache.set(name, value)

    @property
    def cache(self) -> IPCStateCache:
        """Underlying cache instance."""
        return self._cache


# =============================================================================
# Singleton instance
# =============================================================================

from baldur.utils.singleton import CLEANUP_CLOSE, make_singleton_factory

get_cb_state_cache, configure_cb_state_cache, reset_cb_state_cache = (
    make_singleton_factory(
        "cb_state_cache",
        IPCStateCache,
        cleanup_fn=CLEANUP_CLOSE,
    )
)


# Backward-compatible alias (deprecated)
CBStateCache = IPCStateCache
