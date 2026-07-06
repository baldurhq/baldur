"""
Pre-computed Cache Service - Background Pre-computation Worker.

Threading.Timer based lightweight scheduling without Celery dependency.
"""

from __future__ import annotations

import random
import threading
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any

import structlog

from baldur.utils.time import utc_now

from .constants import (
    HAS_CACHETOOLS,
    HAS_ORJSON,
    _get_l1_ttl_seconds,
    _get_l2_ttl_seconds,
    _get_refresh_interval,
    fast_json_dumps,
    record_cache_refresh,
)
from .l1_cache import _l1_cache
from .l2_cache import _l2_cache
from .multi_tier import check_l1_l2_drift

logger = structlog.get_logger()


# =============================================================================
# Background Pre-computation Worker
# =============================================================================


class PrecomputedCacheWorker:
    """
    Background worker that pre-computes cache entries.

    Uses Threading.Timer for lightweight scheduling without Celery dependency.
    Starts automatically via AppConfig.ready().
    """

    def __init__(self):
        self._timer: threading.Timer | None = None
        self._running = False
        self._lock = threading.Lock()
        self._compute_functions: dict[str, Callable[[], dict[str, Any]]] = {}
        self._last_refresh_at: datetime | None = None
        self._started_at: datetime | None = None
        # CB / backoff state (lazy-initialized in start())
        self._cb_service: Any | None = None
        self._backoff: Any | None = None
        self._consecutive_all_failures: int = 0
        self._current_effective_interval: float | None = None
        self._cache_subscribed = False

    def register(
        self, cache_key: str, compute_fn: Callable[[], dict[str, Any]]
    ) -> None:
        """Register a compute function for a cache key."""
        self._compute_functions[cache_key] = compute_fn

    @property
    def cb_service(self) -> Any | None:
        """Public read-only accessor for the circuit breaker service."""
        return self._cb_service

    def start(self) -> None:
        """Start the background worker."""
        with self._lock:
            if self._running:
                return
            self._running = True
            self._started_at = utc_now()

            # Reset mutable state for stop() → start() idempotency
            self._consecutive_all_failures = 0
            self._current_effective_interval = None

            # Lazy initialization of CB and backoff
            from baldur.settings.precomputed_cache import (
                get_precomputed_cache_settings,
            )

            settings = get_precomputed_cache_settings()

            if settings.l3_cb_enabled:
                from baldur.services.circuit_breaker.config import (
                    CircuitBreakerConfig,
                )
                from baldur.services.circuit_breaker.service import (
                    CircuitBreakerService,
                )

                self._cb_service = CircuitBreakerService(
                    config=CircuitBreakerConfig(
                        enabled=True,
                        failure_threshold=settings.l3_cb_failure_threshold,
                        recovery_timeout=settings.l3_cb_recovery_timeout,
                        success_threshold=1,
                        minimum_calls=settings.l3_cb_failure_threshold,
                        failure_rate_threshold=0.0,
                    )
                )

            if settings.jitter_enabled:
                from baldur.core.backoff import DecorrelatedJitterBackoff

                self._backoff = DecorrelatedJitterBackoff(
                    base_delay=settings.refresh_interval_seconds,
                    max_delay=settings.backoff_max_delay_seconds,
                )

            self._register_invalidation_handler()

            logger.info("precomputed_cache.starting_background_worker")

            # Cold start stampede defense
            initial_delay = random.uniform(0, settings.refresh_interval_seconds)
            self._schedule_refresh(delay=initial_delay)

    def stop(self) -> None:
        """Stop the background worker."""
        with self._lock:
            self._running = False
            if self._timer:
                self._timer.cancel()
                self._timer = None
            self._unsubscribe_invalidation_handler()
            logger.info("precomputed_cache.stopped_background_worker")

    def _unsubscribe_invalidation_handler(self) -> None:
        """Unsubscribe EventBus invalidation handler."""
        if not self._cache_subscribed:
            return
        try:
            from baldur.services.event_bus import EventType, get_event_bus

            bus = get_event_bus()
            bus.unsubscribe(
                EventType.PRECOMPUTED_CACHE_INVALIDATED,
                self._on_cache_invalidated,
            )
            self._cache_subscribed = False
        except ImportError:
            pass
        except Exception:
            pass

    def _schedule_refresh(self, delay: float | None = None) -> None:
        """Schedule the next refresh with optional jitter."""
        if not self._running:
            return
        if delay is None:
            base = _get_refresh_interval()
            try:
                from baldur.core.adaptive_jitter import AdaptiveJitter

                jitter = AdaptiveJitter.calculate()
            except ImportError:
                jitter = 0.0
            except Exception:
                logger.debug(
                    "precomputed_cache.jitter_calculation_failed", exc_info=True
                )
                jitter = 0.0
            delay = base + jitter
        self._current_effective_interval = delay
        self._timer = threading.Timer(delay, self._do_refresh)
        self._timer.daemon = True
        self._timer.start()

    def _do_refresh(self) -> None:  # noqa: C901, PLR0912, PLR0915
        """Execute pre-computation for all registered keys with drift detection."""
        start_time = time.perf_counter()
        any_success = False
        cb = self._cb_service
        cb_service_name = "precomputed_cache_compute"

        # CB OPEN → skip all keys
        if cb and not cb.should_allow(cb_service_name):
            logger.debug("precomputed_cache.refresh_skipped_cb_open")
            self._consecutive_all_failures += 1
            if self._backoff:
                delay = self._backoff.calculate(self._consecutive_all_failures)
                # Cap at recovery_timeout so worker probes CB recovery promptly
                from baldur.settings.precomputed_cache import (
                    get_precomputed_cache_settings,
                )

                recovery_timeout = (
                    get_precomputed_cache_settings().l3_cb_recovery_timeout
                )
                delay = min(delay, float(recovery_timeout))
                self._schedule_refresh(delay=delay)
            else:
                self._schedule_refresh()
            return

        for cache_key, compute_fn in self._compute_functions.items():
            try:
                check_l1_l2_drift(cache_key)

                data = compute_fn()
                data["_precomputed_at"] = utc_now().isoformat()
                json_str = fast_json_dumps(data)

                _l1_cache.set(cache_key, json_str)
                _l2_cache.set(cache_key, json_str)

                record_cache_refresh(cache_key, success=True)
                any_success = True

                if cb:
                    try:
                        cb.record_success(cb_service_name)
                    except Exception:
                        pass

            except Exception as e:
                logger.warning(
                    "precomputed_cache.refresh_failed",
                    cache_key=cache_key,
                    error=e,
                )
                record_cache_refresh(cache_key, success=False)

                if cb:
                    try:
                        cb.record_failure(cb_service_name)
                    except Exception:
                        pass

        if any_success or not self._compute_functions:
            self._last_refresh_at = utc_now()

        elapsed_ms = (time.perf_counter() - start_time) * 1000
        logger.debug(
            "precomputed_cache.refresh_completed_ms",
            elapsed_ms=elapsed_ms,
        )

        # Schedule next refresh with backoff logic
        if any_success:
            was_in_backoff = self._consecutive_all_failures > 0
            self._consecutive_all_failures = 0
            if self._backoff:
                self._backoff.reset()
            if was_in_backoff:
                logger.info("precomputed_cache.backoff_recovered")
            self._schedule_refresh()
        else:
            self._consecutive_all_failures += 1
            if self._backoff:
                delay = self._backoff.calculate(self._consecutive_all_failures)
                logger.warning(
                    "precomputed_cache.backoff_activated",
                    consecutive_failures=self._consecutive_all_failures,
                    delay_seconds=delay,
                )
                self._schedule_refresh(delay=delay)
            else:
                self._schedule_refresh()

    def _register_invalidation_handler(self) -> None:
        """Subscribe to EventBus for cross-pod L1 invalidation."""
        if self._cache_subscribed:
            return
        try:
            from baldur.services.event_bus import EventType, get_event_bus

            bus = get_event_bus()
            bus.subscribe(
                EventType.PRECOMPUTED_CACHE_INVALIDATED,
                self._on_cache_invalidated,
            )
            self._cache_subscribed = True
        except ImportError:
            logger.debug("precomputed_cache.eventbus_unavailable")
        except Exception as e:
            logger.warning("precomputed_cache.eventbus_registration_failed", error=e)

    def _on_cache_invalidated(self, event: Any) -> None:
        """Handle cross-pod cache invalidation event."""
        try:
            cache_key = event.data.get("cache_key") if hasattr(event, "data") else None
            if cache_key is not None:
                _l1_cache.invalidate(cache_key)
            else:
                _l1_cache.clear()
            logger.debug("precomputed_cache.l1_invalidated", cache_key=cache_key)
        except Exception as e:
            logger.warning("precomputed_cache.invalidation_handler_failed", error=e)

    def is_running(self) -> bool:
        """Check if worker is running."""
        return self._running

    def get_stats(self) -> dict[str, Any]:
        """Get worker statistics."""
        return {
            "running": self._running,
            "registered_keys": list(self._compute_functions.keys()),
            "refresh_interval_seconds": _get_refresh_interval(),
            "l1_ttl_seconds": _get_l1_ttl_seconds(),
            "l2_ttl_seconds": _get_l2_ttl_seconds(),
            "has_orjson": HAS_ORJSON,
            "has_cachetools": HAS_CACHETOOLS,
        }

    def get_passive_health(self) -> dict[str, Any]:
        """Passive health snapshot for MetaWatchdog probe (no I/O).

        Thread safety: individual field reads are GIL-atomic.
        Cross-field tearing is possible but self-corrects on next probe cycle.
        """
        return {
            "running": self._running,
            "registered_keys": list(self._compute_functions.keys()),
            "last_refresh_at": (
                self._last_refresh_at.isoformat() if self._last_refresh_at else None
            ),
            "started_at": (self._started_at.isoformat() if self._started_at else None),
            "refresh_interval_seconds": _get_refresh_interval(),
            "effective_interval_seconds": (
                self._current_effective_interval or _get_refresh_interval()
            ),
        }


# Global worker instance
_worker = PrecomputedCacheWorker()


def get_precomputed_cache_worker() -> PrecomputedCacheWorker:
    """Get the global pre-computed cache worker."""
    return _worker


def start_precomputed_cache() -> None:
    """Start the pre-computed cache worker."""
    _worker.start()


def stop_precomputed_cache() -> None:
    """Stop the pre-computed cache worker."""
    _worker.stop()


def reset_precomputed_cache_worker() -> None:
    """Stop and reset the global pre-computed cache worker instance."""
    global _worker

    _worker.stop()
    _worker = PrecomputedCacheWorker()
