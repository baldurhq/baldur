"""Shared cache resolver for idempotency decorator and service layers.

Single source of truth for how the idempotency code paths resolve a cache
adapter from :class:`baldur.factory.registry.ProviderRegistry`, with the
production-aware fail-closed semantics introduced by #461 (decorator layer)
and extended by #532 (service layer).

The two callers differ in one respect only:

- ``@idempotent`` decorator: user code calls it directly, so a raised
  ``ConfigurationError`` is visible. Production + no adapter + no escape hatch
  ⇒ **raise**.
- ``IdempotencyService``: called from infrastructure pipelines (audit sync
  worker, cascade auditor, correlation engine) that wrap every call in
  ``except Exception`` by design. Raising would be silenced; the loud signal
  must live in WARN log + Prometheus counter, both of which escape the caller's
  ``except``. Production + no adapter + no escape hatch ⇒ **WARN + counter +
  return in-process fallback** (fail-loud-and-degrade).

The behavioral asymmetry is expressed by the ``raise_on_prod_no_toggle``
parameter on :func:`resolve_cache_via_registry`. This concentration makes the
asymmetry auditable in one place rather than diffused across two files.

The module-level :data:`_SERVICE_FALLBACK_CACHE` is the service-layer's
in-process fallback. The decorator owns its own ``_FALLBACK_CACHE`` with a
distinct ``key_prefix`` so single-worker testbed deployments running both
layers in-process do not collide on keys.

WARN log emission is throttled by an in-process one-shot guard keyed by
``(layer, reason)`` — matches the precedent in
:func:`baldur.factory.registry._warn_if_init_not_called_cache` (no lock; the
race is benign and the Prometheus counter is the cumulative signal).
"""

from __future__ import annotations

import logging
import os
from typing import Any

from baldur.adapters.cache.memory_adapter import InMemoryCacheAdapter
from baldur.core.exceptions import AdapterNotFoundError, ConfigurationError

__all__ = [
    "resolve_cache_via_registry",
    "resolve_async_cache",
    "_SERVICE_FALLBACK_CACHE",
    "_reset_warned_layers",
    "_reset_service_fallback_cache",
]

logger = logging.getLogger(__name__)

# Service-layer module-level fallback. Distinct ``key_prefix`` from the
# decorator's ``_FALLBACK_CACHE`` so the two layers cannot collide on keys
# when both run in-process in a single-worker testbed.
_SERVICE_FALLBACK_CACHE = InMemoryCacheAdapter(key_prefix="idempotency_service:")

# One-shot WARN guard. Keyed by ``(layer, reason)``. ``set.add`` and ``in``
# are GIL-atomic for primitive members under CPython; first-race double-WARN
# is harmless because the Prometheus counter is the cumulative signal and the
# WARN is the human-readable bookmark. Matches the lock-free precedent at
# ``factory/registry.py:_warn_if_init_not_called_cache``.
_fallback_warned_layers: set[tuple[str, str]] = set()


def resolve_cache_via_registry(
    *,
    layer: str,
    fallback_cache: Any,
    raise_on_prod_no_toggle: bool,
) -> Any:
    """Resolve a cache adapter via ProviderRegistry with prod-aware semantics.

    Args:
        layer: ``"decorator"``, ``"service"``, ``"policy"``, ``"singleton"``,
            or ``"recovery_coordinator"`` — identifies the caller in WARN logs,
            the Prometheus counter, and the one-shot guard key.
        fallback_cache: Module-level :class:`InMemoryCacheAdapter` instance
            owned by the caller. Returned when no adapter is registered and
            the production / escape-hatch combination permits fallback.
        raise_on_prod_no_toggle: If True (decorator), production with no
            adapter and no escape hatch raises ``ConfigurationError``. If
            False (service), the same scenario emits WARN +
            Prometheus counter and returns ``fallback_cache``.

    Returns:
        The registered cache adapter, or ``fallback_cache`` when no adapter
        is registered (and policy permits fallback).

    Raises:
        ConfigurationError: Only when ``raise_on_prod_no_toggle=True`` AND
            running in production AND no adapter registered AND escape hatch
            off.
    """
    from baldur.factory.registry import ProviderRegistry
    from baldur.runtime import is_production

    try:
        return ProviderRegistry.get_cache()
    except AdapterNotFoundError:
        pass

    # Cached layered read (686 D3/D5) so a console edit of the idempotency
    # domain's allow_inmemory_fallback is observed within the read-cache TTL; env
    # base when no RuntimeConfigManager is registered.
    from baldur.settings.idempotency import IdempotencySettings
    from baldur.settings.layered_provider import get_layered_settings_cached

    allow_fallback = get_layered_settings_cached(
        IdempotencySettings, "idempotency"
    ).allow_inmemory_fallback
    in_production = is_production()

    if in_production and not allow_fallback:
        reason = "no_cache_adapter_registered"
        _emit_fallback_signal(layer=layer, reason=reason)
        if raise_on_prod_no_toggle:
            # Feature-neutral wording: both the ``@idempotent`` decorator
            # (layer="decorator") and the ``protect(idempotency_key=…)`` facade
            # (layer="policy") raise here, so the message must not name a single
            # surface or it would misdirect a facade operator to a decorator
            # they never wrote.
            raise ConfigurationError(
                "Baldur idempotency requires a cache adapter registered via "
                "ProviderRegistry in production (BALDUR_ENVIRONMENT=production). "
                "Multi-worker deployments would otherwise silently degrade to "
                "per-worker dedup. Register a Redis (or equivalent distributed) "
                "cache adapter, or set "
                "BALDUR_IDEMPOTENCY_ALLOW_INMEMORY_FALLBACK=true to explicitly "
                "accept in-process-only semantics."
            )
        return fallback_cache

    if in_production and allow_fallback:
        _emit_fallback_signal(layer=layer, reason="escape_hatch_enabled")

    return fallback_cache


def resolve_async_cache(
    *,
    layer: str,
    sync_fallback_cache: Any,
    async_fallback_cache: Any,
    raise_on_prod_no_toggle: bool,
) -> Any:
    """Resolve an :class:`AsyncCacheProviderInterface` for the async dedup path.

    Reuses :func:`resolve_cache_via_registry` for the production-fail-closed
    decision (production + no adapter + no escape hatch ⇒ raise
    ``ConfigurationError``) — the fail-closed correctness logic is NOT
    duplicated. It then selects the async backing by inspecting the resolved
    (unwrapped) sync cache's ``provider_name``:

    - ``"redis"`` ⇒ a fresh :class:`AsyncRedisCacheAdapter` (reads the same
      ``get_redis_settings()`` — url + socket timeouts — so it hits the SAME
      Redis keys as the sync adapter ⇒ cross-consistent by construction) whose
      ``aclose()`` is registered with the framework-independent
      ``GracefulShutdownCoordinator`` so the second pool is drained on graceful
      shutdown / process recycle.
    - anything else ⇒ ``async_fallback_cache`` (the async in-memory fallback).

    In-memory-fallback caveat (no Redis registered): the async fallback is a
    SEPARATE in-process store from the sync ``protect()`` fallback, so a
    duplicate protected via BOTH sync ``protect(key=k)`` and async
    ``aprotect(key=k)`` on the same key in the same process may run twice. This
    is production-unreachable — it needs four conjunctive conditions (no Redis +
    single process + same key + one op via both facades); with Redis registered
    both facades hit the SAME keys (this resolver reuses the sync key prefix), so
    they are fully cross-consistent, and production fail-closes without the
    explicit ``BALDUR_IDEMPOTENCY_ALLOW_INMEMORY_FALLBACK`` escape hatch (whose
    "in-process-only" contract already covers this). Workaround: register Redis.

    Args:
        layer: Caller identifier for WARN logs / the Prometheus counter /
            the one-shot guard key (forwarded to the sync resolver).
        sync_fallback_cache: The caller's sync ``InMemoryCacheAdapter``, passed
            to the sync resolver for the prod-decision + provider detection.
        async_fallback_cache: The caller's ``AsyncInMemoryCacheAdapter``,
            returned when no distributed (Redis) adapter is registered.
        raise_on_prod_no_toggle: Forwarded to the sync resolver — production
            with no adapter and no escape hatch raises when True.

    Returns:
        An ``AsyncCacheProviderInterface`` (async Redis or async in-memory).

    Raises:
        ConfigurationError: Propagated from :func:`resolve_cache_via_registry`.
    """
    sync_cache = resolve_cache_via_registry(
        layer=layer,
        fallback_cache=sync_fallback_cache,
        raise_on_prod_no_toggle=raise_on_prod_no_toggle,
    )
    concrete = _unwrap_to_concrete(sync_cache)
    provider = getattr(concrete, "provider_name", "")
    if provider == "redis":
        from baldur.adapters.cache.async_redis_adapter import AsyncRedisCacheAdapter

        adapter = AsyncRedisCacheAdapter()
        _register_async_pool_drain(adapter)
        return adapter
    return async_fallback_cache


def _unwrap_to_concrete(cache: Any) -> Any:
    """Walk ``_delegate`` decorator wrappers to the concrete adapter.

    ``ProviderRegistry.get_cache()`` may return a ``MetricsAwareCacheAdapter``
    wrapping the concrete adapter; its ``provider_name`` delegates, but unwrap
    first so the topology detection reads the real backend regardless.
    """
    seen: set[int] = set()
    while hasattr(cache, "_delegate") and id(cache) not in seen:
        seen.add(id(cache))
        cache = cache._delegate
    return cache


def _register_async_pool_drain(adapter: Any) -> None:
    """Register the async Redis pool's ``aclose()`` as a shutdown handler.

    Framework-independent (atexit + SIGTERM path via
    ``GracefulShutdownCoordinator``), NOT a FastAPI-lifespan-only hook, so the
    second ``redis.asyncio`` pool is drained across every framework adapter.
    Best-effort: if the coordinator has already left the RUNNING phase (or is
    otherwise unavailable), the registration is skipped rather than raising into
    the resolve path.
    """
    try:
        from baldur.core.shutdown_coordinator import (
            ShutdownHandler,
            TrackedRequest,
            get_shutdown_coordinator,
        )

        class _AsyncRedisPoolDrainHandler(ShutdownHandler):
            """Drains the async Redis pool once on drain-complete / force-shutdown.

            The drain runs in the coordinator's background drain thread (no
            running event loop), so ``asyncio.run`` is safe there.
            """

            def __init__(self, drain_adapter: Any) -> None:
                self._adapter = drain_adapter
                self._drained = False

            def on_shutdown_start(self) -> None:
                # Must return quickly; the actual drain is deferred to the
                # background drain thread's on_drain_complete/on_force_shutdown.
                return None

            def on_drain_complete(self) -> None:
                self._drain()

            def on_force_shutdown(self, pending_requests: list[TrackedRequest]) -> None:
                self._drain()

            def _drain(self) -> None:
                if self._drained:
                    return
                self._drained = True
                try:
                    import asyncio

                    asyncio.run(self._adapter.aclose())
                except Exception as e:
                    logger.warning(
                        "idempotency.async_pool_drain_failed",
                        extra={"error": str(e)},
                    )

        get_shutdown_coordinator().register_handler(
            _AsyncRedisPoolDrainHandler(adapter)
        )
    except Exception as e:
        # register_handler raises if shutdown has already started; a
        # best-effort drain must never break dedup-cache resolution.
        logger.debug(
            "idempotency.async_pool_drain_register_skipped",
            extra={"error": str(e)},
        )


def _emit_fallback_signal(*, layer: str, reason: str) -> None:
    """Emit WARN (once per (layer, reason)) + Prometheus counter (always).

    The Prometheus counter is the cumulative SRE signal; the WARN is the
    human-readable bookmark on first occurrence per process.
    """
    key = (layer, reason)
    if key not in _fallback_warned_layers:
        _fallback_warned_layers.add(key)
        event = (
            "idempotency.inmemory_fallback_active"
            if reason == "escape_hatch_enabled"
            else "idempotency.distributed_dedup_unavailable"
        )
        logger.warning(
            event,
            extra={
                "pid": os.getpid(),
                "layer": layer,
                "reason": reason,
            },
        )

    _record_fallback_metric(layer=layer, reason=reason)


def _record_fallback_metric(*, layer: str, reason: str) -> None:
    """Best-effort Prometheus counter increment.

    Mirrors the swallow-on-error pattern used by ``_record_idempotency_check``
    so the resolver hot path is never broken by an observability failure.
    """
    try:
        from baldur.metrics.prometheus import get_metrics

        rec = getattr(get_metrics(), "idempotency", None)
        if rec is not None:
            rec.record_fallback(layer=layer, reason=reason)
    except Exception:
        pass


def _reset_warned_layers() -> None:
    """Test helper — clear the one-shot WARN guard."""
    _fallback_warned_layers.clear()


def _reset_service_fallback_cache() -> None:
    """Test helper — replace ``_SERVICE_FALLBACK_CACHE`` with a fresh instance."""
    global _SERVICE_FALLBACK_CACHE
    _SERVICE_FALLBACK_CACHE = InMemoryCacheAdapter(key_prefix="idempotency_service:")
