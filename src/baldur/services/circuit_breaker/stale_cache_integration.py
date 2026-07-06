"""
Canary + Stale Cache Integration

In the HALF_OPEN state, only requests within the Canary ratio (10%→30%→60%) are
sent to the backend, and the remaining requests immediately return Stale Cache.

Result:
- 90% of users keep using the service without errors (slightly stale data)
- 10% of requests verify backend stability
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Generic, TypeVar

import structlog

from baldur.core.serializable import SerializableMixin
from baldur.services.circuit_breaker.canary_recovery import (
    CanaryRecoveryManager,
    CanaryRecoveryStage,
    get_canary_recovery_manager,
)
from baldur.services.circuit_breaker.config import CircuitState
from baldur.utils.time import utc_now

logger = structlog.get_logger()

T = TypeVar("T")


# =============================================================================
# Stale Cache Configuration
# =============================================================================


@dataclass
class CanaryWithStaleCacheConfig:
    """
    Canary Recovery + Stale Cache combined configuration.

    Attributes:
        enabled: Whether the feature is enabled
        stale_cache_max_age_seconds: Max allowed Stale Cache age (default 5 minutes)
        non_canary_action: How to handle requests excluded from the Canary ratio
        stale_cache_miss_action: How to handle a Stale Cache miss
        add_stale_indicator: Whether to mark the response as Stale
        stale_header_name: Name of the Stale-indicator header
        default_stale_value: Default value on Stale Cache miss (optional)
    """

    enabled: bool = True

    # Stale Cache settings
    stale_cache_max_age_seconds: int = 300  # 5 minutes

    # Handling of requests excluded from the Canary ratio
    non_canary_action: str = "stale_cache"  # "stale_cache" | "reject" | "queue"

    # Fallback when there is no Stale Cache
    stale_cache_miss_action: str = "reject"  # "reject" | "default_value" | "allow"

    # Whether to mark the response as Stale
    add_stale_indicator: bool = True
    stale_header_name: str = "X-Stale-Response"

    # Default value on Stale Cache miss
    default_stale_value: Any | None = None


# =============================================================================
# Stale Cache Entry
# =============================================================================


@dataclass
class StaleCacheEntry(SerializableMixin, Generic[T]):
    """
    Stale Cache entry.

    Attributes:
        key: Cache key
        value: Cached value
        cached_at: Cache time
        service_id: Service ID
        ttl_seconds: Original TTL
    """

    key: str
    value: T
    cached_at: datetime = field(default_factory=lambda: utc_now())
    service_id: str = ""
    ttl_seconds: int = 300  # default 5 minutes

    def age_seconds(self) -> float:
        """Cache age (seconds)."""
        return (utc_now() - self.cached_at).total_seconds()

    def is_stale(self) -> bool:
        """Whether TTL is exceeded (i.e., stale)."""
        return self.age_seconds() > self.ttl_seconds

    def is_expired(self, max_stale_age: int) -> bool:
        """Whether the max stale-allowance time is exceeded."""
        return self.age_seconds() > (self.ttl_seconds + max_stale_age)

    def to_dict(self) -> dict[str, Any]:
        """Convert to a dictionary."""
        return {
            "key": self.key,
            "value": str(self.value)[:100],  # value is summarized
            "cached_at": self.cached_at.isoformat(),
            "service_id": self.service_id,
            "ttl_seconds": self.ttl_seconds,
            "age_seconds": self.age_seconds(),
            "is_stale": self.is_stale(),
        }


# =============================================================================
# Canary with Stale Decision
# =============================================================================


@dataclass
class CanaryWithStaleDecision(SerializableMixin):
    """
    Canary + Stale Cache combined decision result.

    Attributes:
        allow_backend: Whether to allow the backend call
        use_stale: Whether to use Stale Cache
        stale_data: Cached data
        stale_age_seconds: Age of the stale data
        is_canary_request: Whether this is a Canary request
        current_stage: Current Canary stage
        traffic_percent: Traffic ratio of the current stage
        reason: Decision reason
        reject: Whether rejected (no Stale either)
        cb_state: Circuit Breaker state
    """

    allow_backend: bool = False
    use_stale: bool = False
    stale_data: Any | None = None
    stale_age_seconds: float = 0.0
    is_canary_request: bool = False
    current_stage: CanaryRecoveryStage | None = None
    traffic_percent: float = 0.0
    reason: str = ""
    reject: bool = False
    cb_state: str | None = None

    def _post_serialize(self, data: dict[str, Any]) -> dict[str, Any]:
        """Truncate stale_data to str[:100]; None when falsy (original behavior)."""
        if self.stale_data:
            data["stale_data"] = str(self.stale_data)[:100]
        else:
            data["stale_data"] = None
        return super()._post_serialize(data)


# =============================================================================
# Stale Cache Store
# =============================================================================


class StaleCacheStore:
    """
    Stale Cache store.

    Implemented as a simple in-memory cache. In real operation it can be
    replaced with Redis, etc.
    """

    def __init__(self, max_entries: int = 10000):
        """
        Initialize.

        Args:
            max_entries: Maximum number of cache entries
        """
        # Why OrderedDict: a plain dict preserves insertion order on
        # 3.7+ but lacks O(1) move_to_end and FIFO popitem(last=False).
        # Insertion/update order equals age order here because set()
        # always stores a fresh StaleCacheEntry with a new cached_at
        # (moving overwrites to the end) and get() only deletes expired
        # entries - so popitem(last=False) IS oldest-entry eviction.
        self._cache: OrderedDict[str, StaleCacheEntry] = OrderedDict()
        self._max_entries = max_entries
        self._lock = threading.RLock()
        self._stats = {
            "hits": 0,
            "misses": 0,
            "stale_hits": 0,
            "expired": 0,
            "sets": 0,
        }

    def get(
        self,
        key: str,
        max_stale_age: int = 300,
    ) -> StaleCacheEntry | None:
        """
        Look up the cache.

        Args:
            key: Cache key
            max_stale_age: Max stale-allowance time (seconds)

        Returns:
            StaleCacheEntry or None
        """
        with self._lock:
            entry = self._cache.get(key)

            if entry is None:
                self._stats["misses"] += 1
                return None

            # Check expiry
            if entry.is_expired(max_stale_age):
                self._stats["expired"] += 1
                del self._cache[key]
                return None

            # Check whether stale
            if entry.is_stale():
                self._stats["stale_hits"] += 1
            else:
                self._stats["hits"] += 1

            return entry

    def set(
        self,
        key: str,
        value: Any,
        service_id: str = "",
        ttl_seconds: int = 300,
    ) -> StaleCacheEntry:
        """
        Store in the cache.

        Args:
            key: Cache key
            value: Value to cache
            service_id: Service ID
            ttl_seconds: TTL (seconds)

        Returns:
            The created StaleCacheEntry

        Warning:
            Since this is an in-memory cache, the reference to value is stored as-is.
            Modifying the original object after storing also pollutes the cached data.
            The caller must observe one of the following:
            1. Treat the stored object as immutable
            2. Pass a copy via copy.copy() or copy.deepcopy() before storing
        """
        with self._lock:
            # Evict the oldest entry only when inserting a NEW key at
            # capacity - overwriting an existing key replaces in place
            # (no unrelated entry is evicted).
            overwrite = key in self._cache
            if not overwrite and len(self._cache) >= self._max_entries:
                self._evict_oldest()

            entry = StaleCacheEntry(
                key=key,
                value=value,
                service_id=service_id,
                ttl_seconds=ttl_seconds,
            )
            self._cache[key] = entry
            if overwrite:
                # Fresh cached_at -> keep update order equal to age order.
                self._cache.move_to_end(key)
            self._stats["sets"] += 1

            return entry

    def delete(self, key: str) -> bool:
        """Delete from the cache."""
        with self._lock:
            if key in self._cache:
                del self._cache[key]
                return True
            return False

    def _evict_oldest(self) -> None:
        """Evict the oldest entry in O(1) (FIFO head = oldest cached_at)."""
        if not self._cache:
            return

        self._cache.popitem(last=False)

    def clear(self) -> int:
        """Delete the entire cache."""
        with self._lock:
            count = len(self._cache)
            self._cache.clear()
            return count

    def get_stats(self) -> dict[str, Any]:
        """Cache statistics."""
        with self._lock:
            return {
                **self._stats,
                "size": len(self._cache),
                "max_entries": self._max_entries,
            }


# =============================================================================
# Canary with Stale Cache Service
# =============================================================================


class CanaryWithStaleCacheService:
    """
    Canary Recovery + Stale Cache integration service.

    In the HALF_OPEN state, sends only Canary-ratio requests to the backend and
    returns Stale Cache for the rest, minimizing user-facing errors.

    Usage:
        service = CanaryWithStaleCacheService()

        # Check CB state + Canary decision
        decision = service.should_allow_with_fallback(
            service_id="payment-api",
            cache_key="payment:user123",
            cb_state="half_open",
        )

        if decision.allow_backend:
            try:
                result = call_backend()
                # Update the cache on success
                service.update_cache("payment:user123", result)
                service.record_success("payment-api")
            except Exception as e:
                service.record_failure("payment-api")
                raise
        elif decision.use_stale:
            # Return Stale Cache
            return decision.stale_data
        else:
            # Reject
            raise ServiceUnavailable()
    """

    _instance: CanaryWithStaleCacheService | None = None
    _lock: threading.Lock = threading.Lock()

    @staticmethod
    def build_stale_cache_key(domain: str, identifier: str) -> str:
        """
        Centralize the Stale Cache Key generation rule.

        Ensures should_allow_with_fallback(), update_cache(), and the
        FallbackPolicy cache_fn all use the same key.

        Args:
            domain: Service domain (e.g., "payment", "product")
            identifier: Resource identifier (e.g., "user123", "order456")

        Returns:
            Normalized cache key (e.g., "payment:user123")
        """
        return f"{domain}:{identifier}"

    def __new__(cls) -> CanaryWithStaleCacheService:
        """Singleton pattern."""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(
        self,
        config: CanaryWithStaleCacheConfig | None = None,
        canary_manager: CanaryRecoveryManager | None = None,
        cache_store: StaleCacheStore | None = None,
    ):
        """
        Initialize.

        Args:
            config: Stale Cache configuration
            canary_manager: Canary Recovery manager
            cache_store: Stale Cache store
        """
        if getattr(self, "_initialized", False):
            return

        self._config = config or CanaryWithStaleCacheConfig()
        self._canary_manager = canary_manager or get_canary_recovery_manager()
        self._cache = cache_store or StaleCacheStore()
        self._stats = {
            "canary_allowed": 0,
            "stale_served": 0,
            "rejected": 0,
            "backend_success": 0,
            "backend_failure": 0,
        }
        self._stats_lock = threading.Lock()

        self._initialized = True

    # =========================================================================
    # Configuration
    # =========================================================================

    def set_config(self, config: CanaryWithStaleCacheConfig) -> None:
        """Update the configuration."""
        self._config = config

    def get_config(self) -> CanaryWithStaleCacheConfig:
        """Look up the current configuration."""
        return self._config

    # =========================================================================
    # Main Decision Logic
    # =========================================================================

    def should_allow_with_fallback(
        self,
        service_id: str,
        cache_key: str,
        cb_state: str,
    ) -> CanaryWithStaleDecision:
        """
        Canary + Stale Cache combined decision.

        Args:
            service_id: Service ID
            cache_key: Cache key
            cb_state: Current Circuit Breaker state

        Returns:
            CanaryWithStaleDecision
        """
        if not self._config.enabled:
            return CanaryWithStaleDecision(
                allow_backend=True,
                cb_state=cb_state,
                reason="canary+stale disabled",
            )

        # 1. CLOSED: allow normally
        if cb_state == CircuitState.CLOSED or cb_state == "closed":
            return CanaryWithStaleDecision(
                allow_backend=True,
                cb_state=cb_state,
                reason="CB is CLOSED - normal flow",
            )

        # 2. OPEN: use Stale Cache
        if cb_state == CircuitState.OPEN or cb_state == "open":
            return self._handle_open_state(service_id, cache_key, cb_state)

        # 3. HALF_OPEN: apply the Canary ratio
        if cb_state == CircuitState.HALF_OPEN or cb_state == "half_open":
            return self._handle_half_open_state(service_id, cache_key, cb_state)

        # Unknown state
        return CanaryWithStaleDecision(
            allow_backend=True,
            cb_state=cb_state,
            reason=f"unknown CB state: {cb_state}",
        )

    def _handle_open_state(
        self,
        service_id: str,
        cache_key: str,
        cb_state: str,
    ) -> CanaryWithStaleDecision:
        """
        Handle the OPEN state - return Stale Cache.
        """
        stale_entry = self._get_stale_cache(cache_key)

        if stale_entry is not None:
            with self._stats_lock:
                self._stats["stale_served"] += 1

            return CanaryWithStaleDecision(
                allow_backend=False,
                use_stale=True,
                stale_data=stale_entry.value,
                stale_age_seconds=stale_entry.age_seconds(),
                cb_state=cb_state,
                reason="CB is OPEN - returning stale cache",
            )

        # No Stale Cache - handle per configuration
        return self._handle_stale_cache_miss(service_id, cache_key, cb_state)

    def _handle_half_open_state(
        self,
        service_id: str,
        cache_key: str,
        cb_state: str,
    ) -> CanaryWithStaleDecision:
        """
        Handle the HALF_OPEN state - apply the Canary ratio.
        """
        # Request a Canary decision
        canary_decision = self._canary_manager.should_allow_request(service_id)

        if canary_decision.allow_backend:
            # Canary request - allow the backend call
            with self._stats_lock:
                self._stats["canary_allowed"] += 1

            return CanaryWithStaleDecision(
                allow_backend=True,
                is_canary_request=canary_decision.is_canary_request,
                current_stage=canary_decision.current_stage,
                traffic_percent=canary_decision.traffic_percent,
                cb_state=cb_state,
                reason=canary_decision.reason,
            )
        # Non-Canary request - use Stale Cache
        stale_entry = self._get_stale_cache(cache_key)

        if stale_entry is not None:
            with self._stats_lock:
                self._stats["stale_served"] += 1

            return CanaryWithStaleDecision(
                allow_backend=False,
                use_stale=True,
                stale_data=stale_entry.value,
                stale_age_seconds=stale_entry.age_seconds(),
                is_canary_request=False,
                current_stage=canary_decision.current_stage,
                traffic_percent=canary_decision.traffic_percent,
                cb_state=cb_state,
                reason=f"non-canary request, using stale cache (age={stale_entry.age_seconds():.1f}s)",
            )

        # No Stale Cache
        return self._handle_stale_cache_miss(
            service_id,
            cache_key,
            cb_state,
            current_stage=canary_decision.current_stage,
            traffic_percent=canary_decision.traffic_percent,
        )

    def _handle_stale_cache_miss(
        self,
        service_id: str,
        cache_key: str,
        cb_state: str,
        current_stage: CanaryRecoveryStage | None = None,
        traffic_percent: float = 0.0,
    ) -> CanaryWithStaleDecision:
        """
        Handle a Stale Cache Miss.
        """
        action = self._config.stale_cache_miss_action

        if action == "default_value" and self._config.default_stale_value is not None:
            return CanaryWithStaleDecision(
                allow_backend=False,
                use_stale=True,
                stale_data=self._config.default_stale_value,
                stale_age_seconds=0,
                current_stage=current_stage,
                traffic_percent=traffic_percent,
                cb_state=cb_state,
                reason="stale cache miss, using default value",
            )

        if action == "allow":
            return CanaryWithStaleDecision(
                allow_backend=True,
                current_stage=current_stage,
                traffic_percent=traffic_percent,
                cb_state=cb_state,
                reason="stale cache miss, allowing backend call",
            )

        # reject (default)
        with self._stats_lock:
            self._stats["rejected"] += 1

        return CanaryWithStaleDecision(
            allow_backend=False,
            use_stale=False,
            reject=True,
            current_stage=current_stage,
            traffic_percent=traffic_percent,
            cb_state=cb_state,
            reason="stale cache miss, rejecting request",
        )

    def _get_stale_cache(self, cache_key: str) -> StaleCacheEntry | None:
        """Look up the Stale Cache."""
        return self._cache.get(
            key=cache_key,
            max_stale_age=self._config.stale_cache_max_age_seconds,
        )

    # =========================================================================
    # Cache Management
    # =========================================================================

    def update_cache(
        self,
        cache_key: str,
        value: Any,
        service_id: str = "",
        ttl_seconds: int | None = None,
    ) -> StaleCacheEntry:
        """
        Update the cache (store a successful backend response).

        Args:
            cache_key: Cache key
            value: Value to cache
            service_id: Service ID
            ttl_seconds: TTL (uses the configured value if absent)

        Returns:
            The created StaleCacheEntry
        """
        ttl = ttl_seconds or self._config.stale_cache_max_age_seconds
        return self._cache.set(
            key=cache_key,
            value=value,
            service_id=service_id,
            ttl_seconds=ttl,
        )

    def invalidate_cache(self, cache_key: str) -> bool:
        """Invalidate the cache."""
        return self._cache.delete(cache_key)

    def clear_cache(self) -> int:
        """Delete the entire cache."""
        return self._cache.clear()

    # =========================================================================
    # Metrics Recording (Canary integration)
    # =========================================================================

    def record_success(
        self,
        service_id: str,
        cache_key: str | None = None,
        response_data: Any = None,
    ) -> None:
        """
        Record a successful backend call.

        When both cache_key and response_data are provided, update_cache() is
        called automatically to refresh the Stale Cache. A cache-store failure is
        suppressed so it does not affect the original success result.

        Args:
            service_id: Service ID
            cache_key: Stale Cache key (auto-caches when provided)
            response_data: Response data to store in the cache (passed with cache_key)
        """
        with self._stats_lock:
            self._stats["backend_success"] += 1

        # Also record the success in the Canary manager
        self._canary_manager.record_success(service_id)

        # Auto-store the cache — prevents omission
        if cache_key is not None and response_data is not None:
            try:
                self.update_cache(cache_key, response_data, service_id=service_id)
            except Exception as e:
                logger.warning(
                    "auto.cache_update_failed",
                    error=e,
                )

    def record_failure(self, service_id: str) -> None:
        """
        Record a failed backend call.

        Args:
            service_id: Service ID
        """
        with self._stats_lock:
            self._stats["backend_failure"] += 1

        # Also record the failure in the Canary manager
        self._canary_manager.record_failure(service_id)

    # =========================================================================
    # Response Wrapping
    # =========================================================================

    def wrap_response(
        self,
        response: Any,
        decision: CanaryWithStaleDecision,
    ) -> Any:
        """
        Add a Stale indicator to the response.

        Adds a header for an HTTP response, otherwise returns it as-is.

        Args:
            response: Original response
            decision: Canary+Stale decision result

        Returns:
            Response with the Stale indicator added
        """
        if not self._config.add_stale_indicator:
            return response

        if not decision.use_stale:
            return response

        # Add HTTP-response-style headers (Django Response, etc.)
        if hasattr(response, "__setitem__"):
            response[self._config.stale_header_name] = "true"
            response["X-Stale-Age"] = str(int(decision.stale_age_seconds))
        elif hasattr(response, "headers"):
            response.headers[self._config.stale_header_name] = "true"
            response.headers["X-Stale-Age"] = str(int(decision.stale_age_seconds))

        return response

    # =========================================================================
    # Statistics
    # =========================================================================

    def get_stats(self) -> dict[str, Any]:
        """Combined statistics."""
        with self._stats_lock:
            return {
                **self._stats,
                "cache_stats": self._cache.get_stats(),
                "canary_states": self._canary_manager.get_all_recovery_states(),
            }

    def reset_stats(self) -> None:
        """Reset the statistics."""
        with self._stats_lock:
            for key in self._stats:
                self._stats[key] = 0


# =============================================================================
# Module-level Singleton Functions
# =============================================================================


_service_instance: CanaryWithStaleCacheService | None = None
_service_lock = threading.Lock()


def get_canary_stale_cache_service() -> CanaryWithStaleCacheService:
    """Return the singleton instance."""
    global _service_instance
    if _service_instance is None:
        with _service_lock:
            if _service_instance is None:
                _service_instance = CanaryWithStaleCacheService()
    return _service_instance


def reset_canary_stale_cache_service() -> None:
    """Reset the singleton instance (for tests)."""
    global _service_instance
    with _service_lock:
        _service_instance = None
        CanaryWithStaleCacheService._instance = None


# =============================================================================
# Convenience Functions
# =============================================================================


def should_allow_with_fallback(
    service_id: str,
    cache_key: str,
    cb_state: str,
) -> CanaryWithStaleDecision:
    """Canary + Stale Cache combined decision."""
    return get_canary_stale_cache_service().should_allow_with_fallback(
        service_id=service_id,
        cache_key=cache_key,
        cb_state=cb_state,
    )


def update_stale_cache(
    cache_key: str,
    value: Any,
    service_id: str = "",
    ttl_seconds: int | None = None,
) -> StaleCacheEntry:
    """Update the Stale Cache."""
    return get_canary_stale_cache_service().update_cache(
        cache_key=cache_key,
        value=value,
        service_id=service_id,
        ttl_seconds=ttl_seconds,
    )


def record_canary_success(service_id: str) -> None:
    """Record a Canary success."""
    get_canary_stale_cache_service().record_success(service_id)


def record_canary_failure(service_id: str) -> None:
    """Record a Canary failure."""
    get_canary_stale_cache_service().record_failure(service_id)


def build_stale_cache_key(domain: str, identifier: str) -> str:
    """Generate a Stale Cache Key."""
    return CanaryWithStaleCacheService.build_stale_cache_key(domain, identifier)
