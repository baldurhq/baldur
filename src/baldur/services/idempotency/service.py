"""
Idempotency Service

Core service for checking and managing idempotency of operations.

Canonical location: ``baldur.services.idempotency.service``
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

import structlog

from baldur.core.time_provider import get_time_provider

from .models import IdempotencyKey, IdempotencyResult

if TYPE_CHECKING:
    from baldur.core.time_provider import TimeProvider

logger = structlog.get_logger()


def _idempotency_enabled() -> bool:
    """Cached layered idempotency ``enabled`` gate.

    A console edit of the idempotency domain's ``enabled`` flag is observed within
    the read-cache TTL without a restart; the 30s snapshot keeps this off the
    per-op Pydantic-validation hot path. env base when no manager is registered.
    """
    from baldur.settings.idempotency import IdempotencySettings
    from baldur.settings.layered_provider import get_layered_settings_cached

    return get_layered_settings_cached(IdempotencySettings, "idempotency").enabled


def _record_idempotency_check(result: str, domain: str) -> None:
    """Emit baldur_idempotency_check_total{result, domain} on each check outcome.

    Best-effort: any error inside the metrics path is swallowed so the
    idempotency hot path is never broken by an observability failure.
    """
    try:
        from baldur.metrics.prometheus import get_metrics

        rec = getattr(get_metrics(), "idempotency", None)
        if rec is not None:
            rec.record_check(result=result, domain=domain)
    except Exception:
        pass


class IdempotencyService:
    """
    Service for checking and managing idempotency of operations.

    Provides both cache-based (fast) and database-based (reliable)
    idempotency checking.

    For framework-agnostic usage, provide lookup callbacks when calling check().

    Example:
        # Framework-agnostic usage with TimeProvider
        from baldur.core.time_provider import MockTimeProvider

        service = IdempotencyService(time_provider=MockTimeProvider())
        key = IdempotencyKey.for_operation("order", 123, "process")
        result = service.check(key, lookup_fn=my_lookup)
    """

    def __init__(
        self,
        cache_ttl: int | None = None,
        time_provider: TimeProvider | None = None,
        clock_skew_tolerance_seconds: float | None = None,
    ):
        """
        Initialize the idempotency service.

        Args:
            cache_ttl: Custom cache TTL in seconds
            time_provider: TimeProvider for testable time operations
            clock_skew_tolerance_seconds: Clock skew tolerance for distributed checks
        """

        # Layered read (686 D3/D5) so a console edit of the idempotency domain is
        # observed. These TTL/tolerance fields are captured at construction (the
        # module singleton captures once), so their reach is after-worker-restart;
        # env base when no RuntimeConfigManager is registered.
        from baldur.settings.idempotency import IdempotencySettings
        from baldur.settings.layered_provider import get_layered_settings_cached

        idem = get_layered_settings_cached(IdempotencySettings, "idempotency")
        self._default_cache_ttl = idem.default_cache_ttl
        self._extended_cache_ttl = idem.extended_cache_ttl
        self.cache_ttl = cache_ttl or self._default_cache_ttl

        # Clock skew tolerance
        self._clock_skew_tolerance = (
            clock_skew_tolerance_seconds
            if clock_skew_tolerance_seconds is not None
            else idem.clock_skew_tolerance_seconds
        )
        self._time_provider: TimeProvider = time_provider or get_time_provider()
        self._cache = None  # Lazy initialized
        self._held_locks: dict[str, Any] = {}  # key_str -> DistributedLock instance

    def _get_cache(self):
        """Lazy-load a CacheProviderInterface via the shared resolver.

        Behavior (mirrors :func:`baldur.services.idempotency._cache_resolver.
        resolve_cache_via_registry`, with ``raise_on_prod_no_toggle=False``):

        - Adapter registered → return it.
        - No adapter + production + ``BALDUR_IDEMPOTENCY_ALLOW_INMEMORY_FALLBACK=false``
          → emit one-shot WARN ``idempotency.distributed_dedup_unavailable`` +
          increment ``baldur_idempotency_cache_unavailable_fallback_total{layer="service",
          reason="no_cache_adapter_registered"}`` and return the module-level
          in-process fallback. **Does not raise** — every internal caller
          (audit sync_worker, cascade auditor, correlation engine) is fail-open
          by design and would silence a raised exception, leaving operators
          with zero signal. The Prometheus counter is the SRE-visible channel.
        - No adapter + production + escape hatch on → WARN
          ``idempotency.inmemory_fallback_active`` + counter + fallback.
        - No adapter + non-production → silent fallback (no WARN, no counter).
        """
        from baldur.services.idempotency._cache_resolver import (
            _SERVICE_FALLBACK_CACHE,
            resolve_cache_via_registry,
        )

        if self._cache is None:
            self._cache = resolve_cache_via_registry(
                layer="service",
                fallback_cache=_SERVICE_FALLBACK_CACHE,
                raise_on_prod_no_toggle=False,
            )
        return self._cache

    @property
    def DEFAULT_CACHE_TTL(self) -> int:
        """Default TTL for cache-based idempotency."""
        return self._default_cache_ttl

    @property
    def EXTENDED_CACHE_TTL(self) -> int:
        """Extended TTL for operations requiring longer TTL."""
        return self._extended_cache_ttl

    @property
    def clock_skew_tolerance(self) -> float:
        """Clock skew tolerance in seconds for distributed checks."""
        return self._clock_skew_tolerance

    @property
    def time_provider(self) -> TimeProvider:
        """Get the time provider for this service."""
        return self._time_provider

    def now(self) -> datetime:
        """
        Get current time using the configured TimeProvider.

        Returns:
            Current datetime from time provider
        """
        return self._time_provider.now()

    def check(
        self,
        key: IdempotencyKey,
        lookup_fn: Callable[..., Any] | None = None,
        cache_ttl: int | None = None,
    ) -> IdempotencyResult:
        """
        Check if an operation has already been processed.

        Args:
            key: The idempotency key to check
            lookup_fn: Optional callback to check database
            cache_ttl: Optional custom TTL for cache

        Returns:
            IdempotencyResult with duplicate status

        Note:
            Gracefully degrades to DB-only check if Redis is unavailable.
            When ``BALDUR_IDEMPOTENCY_ENABLED=false``, returns
            ``IdempotencyResult(is_duplicate=False, message="idempotency disabled")``
            without consulting cache or database.
        """
        if not _idempotency_enabled():
            _record_idempotency_check("disabled", key.domain.value)
            return IdempotencyResult(
                is_duplicate=False,
                message="idempotency disabled",
            )

        ttl = cache_ttl or self.cache_ttl
        cache = self._get_cache()

        # Check cache first (fast path) with graceful degradation
        try:
            cached_value = cache.get(key.cache_key)
            if cached_value:
                logger.debug(
                    "idempotency.cache_hit",
                    idempotency_key=key.key,
                )
                _record_idempotency_check("cache_hit", key.domain.value)
                return IdempotencyResult(
                    is_duplicate=True,
                    existing_record=cached_value,
                    message="Found in cache",
                )
        except Exception as e:
            logger.warning(
                "idempotency.cache_unavailable_falling_back",
                error=e,
            )

        # Check database if lookup provided
        if lookup_fn:
            try:
                existing = lookup_fn(**key.components)
                if existing:
                    # Update cache for future lookups (best-effort)
                    try:
                        record_id = getattr(existing, "id", existing)
                        cache.set(key.cache_key, record_id, ttl=timedelta(seconds=ttl))
                    except Exception:
                        pass
                    logger.debug(
                        "idempotency.db_hit",
                        idempotency_key=key.key,
                    )
                    _record_idempotency_check("db_hit", key.domain.value)
                    return IdempotencyResult(
                        is_duplicate=True,
                        existing_record=existing,
                        message="Found in database",
                    )
            except Exception as e:
                logger.warning(
                    "idempotency.lookup_failed",
                    error=e,
                )

        _record_idempotency_check("miss", key.domain.value)
        return IdempotencyResult(
            is_duplicate=False,
            message="Not found",
        )

    def mark_as_processed(
        self,
        key: IdempotencyKey,
        record_id: int | None = None,
        ttl: int | None = None,
    ) -> bool:
        """
        Mark an operation as processed in the cache.

        Call this after successfully completing an operation.

        Args:
            key: The idempotency key
            record_id: Optional record ID to cache
            ttl: Optional custom TTL

        Returns:
            True if cache was updated, False if cache was unavailable.
            The operation is still considered successful even if cache fails,
            as the DB is the source of truth. When idempotency is globally
            disabled, returns True without writing to cache.
        """
        if not _idempotency_enabled():
            return True

        cache = self._get_cache()
        value = record_id if record_id else True
        try:
            cache.set(
                key.cache_key, value, ttl=timedelta(seconds=ttl or self.cache_ttl)
            )
            logger.debug(
                "idempotency.marked_processed",
                idempotency_cache_key=key.cache_key,
            )
            return True
        except Exception as e:
            # Redis unavailable - log but don't fail the operation
            logger.warning(
                "idempotency.mark_processed_cache_failed",
                error=e,
            )
            return False

    def batch_check(self, keys: list[IdempotencyKey]) -> list[IdempotencyResult]:
        """
        Batch idempotency check using cache get_many (Redis MGET).

        Args:
            keys: List of idempotency keys to check

        Returns:
            List of IdempotencyResult in the same order as input keys.
            When idempotency is globally disabled, returns all
            ``is_duplicate=False`` results without consulting cache.
        """
        if not keys:
            return []

        if not _idempotency_enabled():
            return [
                IdempotencyResult(is_duplicate=False, message="idempotency disabled")
                for _ in keys
            ]

        cache = self._get_cache()
        cache_keys = [k.cache_key for k in keys]

        try:
            cached_values = cache.mget(cache_keys)
        except Exception as e:
            logger.warning(
                "idempotency.batch_check_cache_unavailable",
                error=e,
                batch_size=len(keys),
            )
            cached_values = {}

        results = []
        for cache_key in cache_keys:
            if cache_key in cached_values:
                results.append(
                    IdempotencyResult(
                        is_duplicate=True,
                        existing_record=cached_values[cache_key],
                        message="Found in cache (batch)",
                    )
                )
            else:
                results.append(
                    IdempotencyResult(
                        is_duplicate=False,
                        message="Not found",
                    )
                )

        return results

    def batch_mark_as_processed(
        self,
        keys: list[IdempotencyKey],
        ttl: int | None = None,
    ) -> bool:
        """
        Batch mark operations as processed using cache set_many (Redis Pipeline SET).

        Args:
            keys: List of idempotency keys to mark
            ttl: Optional custom TTL

        Returns:
            True if cache was updated, False if cache was unavailable.
            When idempotency is globally disabled, returns True without
            writing to cache.
        """
        if not keys:
            return True

        if not _idempotency_enabled():
            return True

        cache = self._get_cache()
        timeout = ttl or self.cache_ttl
        mapping = {k.cache_key: True for k in keys}

        try:
            cache.mset(mapping, ttl=timedelta(seconds=timeout))
            logger.debug(
                "idempotency.batch_marked_processed",
                batch_size=len(keys),
            )
            return True
        except Exception as e:
            logger.warning(
                "idempotency.batch_mark_processed_cache_failed",
                error=e,
                batch_size=len(keys),
            )
            return False

    def acquire_lock(self, key: IdempotencyKey, ttl_seconds: int) -> bool:
        """Acquire a distributed lock for the given idempotency key.

        Delegates to CacheProviderInterface.get_lock() → DistributedLock.
        When no cache adapter is registered, the resolver returns an
        in-process InMemoryCacheAdapter whose ``get_lock`` is single-process
        only — sufficient for single-worker installs; multi-worker
        deployments must register Redis (enforced at init time).

        Args:
            key: Idempotency key identifying the lock scope
            ttl_seconds: Lock auto-expiry in seconds (prevents deadlock on crash)

        Returns:
            True if lock acquired, False if already held by another process.
        """
        cache = self._get_cache()
        lock_name = f"idempotency:lock:{key.domain}:{key.key}"
        lock = cache.get_lock(
            lock_name,
            timeout=timedelta(seconds=ttl_seconds),
        )
        if lock.acquire(blocking=False):
            self._held_locks[f"{key.domain}:{key.key}"] = lock
            return True
        return False

    def release_lock(self, key: IdempotencyKey) -> bool:
        """Release a previously acquired distributed lock.

        Best-effort: if release fails (network issue), TTL auto-expires the lock.

        Args:
            key: Idempotency key identifying the lock scope

        Returns:
            True if lock was found and release attempted, False if not held.
        """
        lock = self._held_locks.pop(f"{key.domain}:{key.key}", None)
        if lock is not None:
            try:
                lock.release()
            except Exception:
                pass  # Best-effort; TTL auto-expires
            return True
        return False

    def clear(self, key: IdempotencyKey) -> bool:
        """
        Clear an idempotency key from cache.

        Use with caution - only for cleanup or testing.

        Args:
            key: The idempotency key to clear

        Returns:
            True if cache was cleared, False if cache was unavailable.
        """
        cache = self._get_cache()
        try:
            cache.delete(key.cache_key)
            logger.debug(
                "idempotency.cleared",
                idempotency_cache_key=key.cache_key,
            )
            return True
        except Exception as e:
            logger.warning(
                "idempotency.clear_key_cache_failed",
                error=e,
            )
            return False


# Singleton instance
_service: IdempotencyService | None = None
_service_lock = threading.Lock()


def get_idempotency_service() -> IdempotencyService:
    """Get the singleton IdempotencyService instance."""
    global _service
    if _service is None:
        with _service_lock:
            if _service is None:
                _service = IdempotencyService()
    return _service


def reset_idempotency_service() -> None:
    """Reset the singleton IdempotencyService instance."""
    global _service
    _service = None
