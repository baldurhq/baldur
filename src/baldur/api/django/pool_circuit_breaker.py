"""
Pool-aware Circuit Breaker Middleware for Django.

Returns 503 immediately when the pool is exhausted to prevent a system stall.
Uses a Fail-Fast strategy instead of blocking on the pool.

Core principle:
1. Check pool status on request arrival (non-blocking, cache-based)
2. Return 503 immediately when the pool is exhausted (does not wait on the pool!)
3. Manage as Circuit Breaker state for automatic recovery

v6.2.0 (2026-01-01): blocking issue resolved
- Per-request check_pool_status() call -> changed to a cache-based lookup
- A background thread periodically refreshes the pool status (default: 100ms)
- Atomic-read pattern applied to minimize lock contention

v6.2.1 (2026-01-02): enterprise-grade stability hardening
- TTL (cache refresh interval) range validation: auto-clamped to 50ms ~ 1000ms
- Stale data handling: warn at 10x threshold, safe fallback beyond 5 seconds
- Automatic background-thread restart detection (is_alive check)
- Audit integration: records decision_source: "cached_pool_status" on rejection decisions
- New statistics counters: stale_cache_fallbacks, stale_cache_warnings, background_thread_restarts
"""

import os
import threading
import time
from typing import Any

import structlog
from django.db import connections
from django.http import JsonResponse

from baldur.interfaces.repositories import CircuitBreakerStateEnum

# Drift Detection metrics
from baldur.metrics.drift_metrics import (
    record_pool_cb_background_restart,
    record_pool_cb_cache_age,
    record_pool_cb_stale,
    update_pool_cb_hit_rate,
)
from baldur.settings.pool_circuit_breaker import get_pool_circuit_breaker_settings

logger = structlog.get_logger()


class PoolCircuitBreaker:
    """
    Circuit Breaker based on pool status.

    States (canonical ``CircuitBreakerStateEnum`` values):
    - ``closed``: normal - all requests allowed
    - ``open``: exhausted - all requests rejected immediately (503)
    - ``half_open``: recovery testing - only some requests allowed
    """

    # Singleton instance
    _instance = None
    _lock = threading.Lock()

    # State constants (canonical CircuitBreakerStateEnum values)
    CLOSED = CircuitBreakerStateEnum.CLOSED.value  # Normal
    OPEN = CircuitBreakerStateEnum.OPEN.value  # Blocking
    HALF_OPEN = CircuitBreakerStateEnum.HALF_OPEN.value  # Recovery testing

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return

        self._initialized = True
        self._state: str = self.CLOSED
        self._state_lock = threading.Lock()

        # Settings resolved through the BALDUR_POOL_CB_ Pydantic settings layer.
        # Range validation (incl. the cache-interval bounds) is enforced at
        # settings load, so an out-of-range value fails loudly here instead of
        # being silently clamped.
        settings = get_pool_circuit_breaker_settings()
        self._failure_threshold = settings.failure_threshold  # OPEN after N failures
        self._success_threshold = settings.success_threshold  # CLOSED after N successes
        self._recovery_timeout = settings.recovery_timeout  # HALF_OPEN after N seconds
        self._half_open_max_requests = settings.half_open_max_requests

        # v6.2.0: cache-based pool status lookup settings
        self._cache_interval_ms = settings.cache_interval_ms

        # v6.2.1: stale cache threshold settings
        self._stale_threshold_multiplier = settings.stale_multiplier
        self._critical_stale_ms = settings.critical_stale_ms

        self._cached_pool_status = {
            "available": False,
            "reason": "Not initialized yet",
            "is_exhausted": False,
            "is_near_exhaustion": False,
            "_cache_time": 0,
            "_is_stale": True,  # v6.2.1: stale initially
        }
        # Lock for cache refresh (separate from request handling)
        self._cache_lock = threading.Lock()
        self._background_thread = None
        self._stop_background = threading.Event()
        self._handle = None  # DaemonWorkerHandle (impl 489 D9)
        self._last_successful_refresh = 0  # v6.2.1: last successful refresh time

        # State tracking
        self._failure_count = 0
        self._success_count = 0
        self._last_failure_time = None
        self._open_time = None
        self._half_open_requests = 0

        # Statistics
        self._stats = {
            "total_requests": 0,
            "rejected_requests": 0,
            "pool_exhaustion_count": 0,
            "recovery_count": 0,
            "state_changes": [],
            "cache_hits": 0,
            "cache_refreshes": 0,
            "stale_cache_fallbacks": 0,  # v6.2.1: safe fallback count due to stale
            "stale_cache_warnings": 0,  # v6.2.1: stale warning count
            "background_thread_restarts": 0,  # v6.2.1: thread restart count
        }

        # The background pool-status refresh thread is intentionally NOT started
        # here. Starting it in __init__ made it an import-time side effect (the
        # module-level singleton below constructs eagerly), which spawned a
        # doomed thread in any process that merely imported this module without a
        # configured Django. It is now started explicitly by the enabled
        # PoolCircuitBreakerMiddleware (the sole consumer of the cached status);
        # get_cached_pool_status() still self-heals a dead thread as a safety net.

        logger.info(
            "pool_circuit_breaker.initialized_fail_fast_enabled",
            cache_interval_ms=self._cache_interval_ms,
        )

    @property
    def state(self) -> str:
        """Current state."""
        with self._state_lock:
            return self._state

    def _set_state(self, new_state: str):
        """Change state."""
        with self._state_lock:
            if self._state != new_state:
                old_state = self._state
                self._state = new_state

                timestamp = time.time()
                self._stats["state_changes"].append(
                    {
                        "from": old_state,
                        "to": new_state,
                        "time": timestamp,
                    }
                )

                logger.warning(
                    "pool_circuit_breaker.state",
                    old_state=old_state,
                    new_state=new_state,
                )

                if new_state == self.OPEN:
                    self._open_time = timestamp
                    self._stats["pool_exhaustion_count"] += 1
                elif new_state == self.CLOSED and old_state != self.CLOSED:
                    self._stats["recovery_count"] += 1

    # =========================================================================
    # v6.2.0: cache-based pool status lookup (Non-Blocking)
    # =========================================================================

    def _start_background_refresh(self):
        """Start the background pool-status refresh thread."""
        from baldur.meta.daemon_worker import DaemonWorkerHandle
        from baldur.metrics.recorders.daemon_worker import register_daemon_worker

        if self._background_thread and self._background_thread.is_alive():
            return  # Already running

        self._stop_background.clear()
        self._spawn_background_thread()
        if self._handle is None:
            self._handle = DaemonWorkerHandle(
                thread=self._background_thread,
                tick_interval_seconds=self._cache_interval_ms / 1000.0,
                restart_callback=self._spawn_background_thread,
            )
            register_daemon_worker("PoolCB-Refresh", self._handle)
        else:
            self._handle.thread = self._background_thread
        logger.debug("pool_circuit_breaker.background_refresh_thread_started")

    def _spawn_background_thread(self) -> None:
        """Construct + start a fresh background refresh thread (impl 489 D9)."""
        self._background_thread = threading.Thread(
            target=self._background_refresh_loop_with_crash_capture,
            name="PoolCB-Refresh",
            daemon=True,
        )
        self._background_thread.start()
        if self._handle is not None:
            self._handle.thread = self._background_thread

    def _background_refresh_loop_with_crash_capture(self) -> None:
        try:
            self._background_refresh_loop()
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as e:
            if self._handle is not None:
                self._handle.record_crash(e)
            raise

    def _stop_background_refresh(self):
        """Stop the background refresh thread."""
        from baldur.metrics.recorders.daemon_worker import unregister_daemon_worker

        if self._handle is not None:
            self._handle.is_stopping = True
        self._stop_background.set()
        if self._background_thread:
            self._background_thread.join(timeout=1.0)
            unregister_daemon_worker("PoolCB-Refresh")
            if self._background_thread.is_alive():
                logger.critical(
                    "daemon_worker.stop_join_timeout",
                    worker_name="PoolCB-Refresh",
                    join_timeout_seconds=1.0,
                )
            logger.debug("pool_circuit_breaker.background_refresh_thread_stopped")

    def _background_refresh_loop(self):
        """Periodically refresh the pool status in the background."""
        interval_sec = self._cache_interval_ms / 1000.0
        consecutive_failures = 0

        while not self._stop_background.is_set():
            iter_start = time.monotonic()
            try:
                # Pool status lookup (only this part is potentially blocking)
                new_status = self._fetch_pool_status_internal()
                current_time = time.time()
                new_status["_cache_time"] = current_time
                new_status["_is_stale"] = False  # v6.2.1: fresh data

                # Cache update (close to an atomic swap)
                with self._cache_lock:
                    self._cached_pool_status = new_status
                    self._stats["cache_refreshes"] += 1
                    self._last_successful_refresh = current_time

                # Reset the consecutive-failure counter on success
                consecutive_failures = 0

            except Exception as e:
                # v6.2.1: track consecutive failures
                consecutive_failures += 1
                logger.debug(
                    "pool_circuit_breaker.background_refresh_failed",
                    consecutive_failures=consecutive_failures,
                    error=e,
                )

                # Warn on 5 consecutive failures (5 * 100ms = no refresh for 500ms+)
                if consecutive_failures >= 5:
                    logger.warning(
                        "pool_circuit_breaker.background_refresh_failing_consecutively",
                        consecutive_failures=consecutive_failures,
                    )

            if self._handle is not None:
                self._handle.observe_iteration(time.monotonic() - iter_start)
                self._handle.heartbeat()

            # Wait until the next refresh
            self._stop_background.wait(timeout=interval_sec)

    def get_cached_pool_status(self) -> dict:
        """
        Return the cached pool status (Non-Blocking).

        v6.2.0: called on every request to avoid blocking.
        v6.2.1: adds stale-cache detection and safe-fallback handling.
        v6.2.2: Prometheus metric integration.

        Stale handling policy:
        - Warning (exceeds stale_threshold_multiplier): log a warning, use cached data
        - Fallback (exceeds critical_stale_ms): safely fall back to CLOSED (allow requests)
        """
        # Read from cache (very fast)
        with self._cache_lock:
            status: dict[str, Any] = self._cached_pool_status.copy()
            self._stats["cache_hits"] += 1

        # v6.2.1: check background-thread status and auto-restart
        if not self._background_thread or not self._background_thread.is_alive():
            if self._background_thread is None:
                # First lazy start: a consumer reached the cache before the
                # middleware started the thread. Not a death — no restart metric.
                logger.debug("pool_circuit_breaker.background_refresh_lazy_started")
            else:
                # The thread was running and died — a genuine restart.
                logger.error("pool_circuit_breaker.background_thread_died_restarting")
                self._stats["background_thread_restarts"] += 1
                # v6.2.2: record Prometheus metric
                record_pool_cb_background_restart()
            self._start_background_refresh()

        # v6.2.1: cache validity check (staged handling)
        cache_age_ms = (time.time() - status.get("_cache_time", 0)) * 1000
        stale_warning_threshold = (
            self._cache_interval_ms * self._stale_threshold_multiplier
        )

        # v6.2.2: record cache-age histogram
        record_pool_cb_cache_age(cache_age_ms)

        # v6.2.2: update cache hit rate
        total_accesses = self._stats.get("cache_hits", 0) + self._stats.get(
            "cache_misses", 0
        )
        if total_accesses > 0:
            hit_rate = self._stats.get("cache_hits", 0) / total_accesses
            update_pool_cb_hit_rate(hit_rate)

        if cache_age_ms > self._critical_stale_ms:
            # Critical Stale: fully outdated cache -> safely fall back to CLOSED
            logger.error(
                "pool_circuit_breaker.critical_stale_cache_ms",
                cache_age_ms=cache_age_ms,
                critical_stale_ms=self._critical_stale_ms,
            )
            self._stats["stale_cache_fallbacks"] += 1
            # v6.2.2: record Prometheus metric
            record_pool_cb_stale("critical")
            # Safe fallback: assume the pool is healthy (allow requests)
            return {
                "available": True,
                "is_exhausted": False,
                "is_near_exhaustion": False,
                "_cache_time": status.get("_cache_time", 0),
                "_is_stale": True,
                "_stale_fallback": True,  # For audit: marks this as a fallback-driven decision
                "_cache_age_ms": cache_age_ms,
            }

        if cache_age_ms > stale_warning_threshold:
            # Warning Stale: log a warning only and use cached data
            logger.warning(
                "pool_circuit_breaker.stale_cache_ms_old",
                cache_age_ms=cache_age_ms,
                stale_warning_threshold=stale_warning_threshold,
            )
            self._stats["stale_cache_warnings"] += 1
            # v6.2.2: record Prometheus metric
            record_pool_cb_stale("warning")
            status["_is_stale"] = True
            status["_cache_age_ms"] = cache_age_ms

        return status

    def check_pool_status(self) -> dict:
        """
        Look up the pool status (cache first).

        v6.2.0: returns the cached value by default (Non-Blocking).
        Use _fetch_pool_status_internal() when a real-time lookup is needed.
        """
        return self.get_cached_pool_status()

    def _fetch_pool_status_internal(self) -> dict:
        """
        Actual pool status lookup (internal, potentially blocking).

        Called only from the background thread.
        """
        try:
            # Access via django-db-connection-pool's pool_container
            try:
                from dj_db_conn_pool.core.mixins.core import pool_container

                has_pool = pool_container.has("default")

                if has_pool:
                    pool = pool_container.get("default")
                    pool_size = pool.size()
                    checkedout = pool.checkedout()
                    checkedin = pool.checkedin()
                    overflow = pool.overflow()
                    max_overflow = getattr(pool, "_max_overflow", 2)
                    total_capacity = pool_size + max_overflow

                    # Pool exhaustion determination:
                    # With pool size 3 and max_overflow 0:
                    # - checkedout >= 3 means fully exhausted
                    # - checkedin == 0 means no available connections
                    is_at_capacity = checkedout >= total_capacity
                    is_overflow_maxed = (
                        overflow >= max_overflow if max_overflow > 0 else True
                    )
                    is_no_available = checkedin == 0

                    # More aggressive exhaustion detection: zero available connections and all in use
                    is_exhausted = is_no_available and checkedout >= pool_size

                    # Near exhaustion at 66%+ usage (2 of 3 pool connections in use)
                    is_near_exhaustion = checkedout >= total_capacity * 0.66

                    if is_exhausted:
                        logger.warning(
                            "pool_circuit_breaker.exhausted",
                            checkedout=checkedout,
                            checkedin=checkedin,
                            pool_size=pool_size,
                        )

                    return {
                        "available": True,
                        "pool_size": pool_size,
                        "checkedout": checkedout,
                        "checkedin": checkedin,
                        "overflow": overflow,
                        "max_overflow": max_overflow,
                        "total_capacity": total_capacity,
                        "usage_percent": (
                            (checkedout / total_capacity * 100)
                            if total_capacity > 0
                            else 0
                        ),
                        "is_exhausted": is_exhausted,
                        "is_near_exhaustion": is_near_exhaustion,
                        # For debugging
                        "_is_at_capacity": is_at_capacity,
                        "_is_overflow_maxed": is_overflow_maxed,
                        "_is_no_available": is_no_available,
                    }
                # pool_container is empty - try direct access via the Django connection
                # but do not call ensure_connection() (avoid blocking)
                conn = connections["default"]
                # Access the pool only if a connection already exists
                if hasattr(conn, "connection") and conn.connection is not None:
                    raw_conn = conn.connection
                    if hasattr(raw_conn, "_pool"):
                        pool = raw_conn._pool
                        pool_size = pool.size()
                        checkedout = pool.checkedout()
                        checkedin = pool.checkedin()
                        overflow = pool.overflow()
                        max_overflow = getattr(pool, "_max_overflow", 0)
                        total_capacity = pool_size + max_overflow

                        is_exhausted = checkedin == 0 and checkedout >= pool_size
                        is_near_exhaustion = checkedout >= total_capacity * 0.66

                        if is_exhausted:
                            logger.warning(
                                "pool_circuit_breaker.exhausted",
                                checkedout=checkedout,
                                checkedin=checkedin,
                                pool_size=pool_size,
                            )

                        return {
                            "available": True,
                            "pool_size": pool_size,
                            "checkedout": checkedout,
                            "checkedin": checkedin,
                            "overflow": overflow,
                            "max_overflow": max_overflow,
                            "total_capacity": total_capacity,
                            "usage_percent": (
                                (checkedout / total_capacity * 100)
                                if total_capacity > 0
                                else 0
                            ),
                            "is_exhausted": is_exhausted,
                            "is_near_exhaustion": is_near_exhaustion,
                        }

                # No connection yet - no pool either (normal, before the first request)
                return {
                    "available": False,
                    "reason": "Pool not initialized yet",
                    "is_exhausted": False,
                    "is_near_exhaustion": False,
                }
            except ImportError as e:
                # django-db-connection-pool not installed
                logger.warning(
                    "pool_circuit_breaker.available",
                    error=e,
                )
                return {
                    "available": False,
                    "reason": "dj_db_conn_pool not installed",
                    "is_exhausted": False,
                    "is_near_exhaustion": False,
                }

            # Fallback: no pool_container and the import also failed
            # Return immediately without attempting a connection (ensure_connection removed!)
            return {
                "available": False,
                "reason": "No pool available",
                "is_exhausted": False,
                "is_near_exhaustion": False,
            }

        except Exception as e:
            logger.exception(
                "pool_circuit_breaker.pool_status_check_failed",
                error=e,
            )
            return {
                "available": False,
                "reason": str(e),
            }

    def should_allow_request(self) -> tuple[bool, str | None]:  # noqa: C901, PLR0912
        """
        Decide whether to allow the request (Non-Blocking).

        v6.2.0: uses the cached pool status to avoid blocking.

        Returns:
            (allow: bool, reason: Optional[str])
        """
        self._stats["total_requests"] += 1

        with self._state_lock:
            current_state = self._state

            if current_state == self.CLOSED:
                # Normal state - check the cached pool status (Non-Blocking!)
                pool_status = self.get_cached_pool_status()

                if pool_status.get("is_exhausted"):
                    # Pool exhaustion detected! Transition to OPEN immediately
                    logger.error(
                        "pool_circuit_breaker.pool_exhausted",
                        pool_status=pool_status.get("checkedout"),
                        total_capacity=pool_status.get("total_capacity"),
                    )
                    self._set_state(self.OPEN)
                    self._stats["rejected_requests"] += 1
                    self._stats["pool_exhaustion_count"] += 1
                    return (False, "Pool exhausted - Circuit OPEN")

                if pool_status.get("is_near_exhaustion"):
                    # 80%+ in use - warn and increment the failure count
                    self._failure_count += 1
                    usage = pool_status.get("usage_percent", 0)
                    logger.warning(
                        "pool_circuit_breaker.pool_usage_high_failures",
                        usage=usage,
                        failure_count=self._failure_count,
                        failure_threshold=self._failure_threshold,
                    )

                    # OPEN when the threshold is reached
                    if self._failure_count >= self._failure_threshold:
                        self._set_state(self.OPEN)
                        self._stats["rejected_requests"] += 1
                        return (
                            False,
                            f"Pool near exhaustion ({usage:.1f}%) - Circuit OPEN",
                        )

                    # At 90%+, reject with 50% probability (load shedding)
                    if usage >= 90:
                        import random

                        if random.random() < 0.5:
                            self._stats["rejected_requests"] += 1
                            return (
                                False,
                                f"Pool critical ({usage:.1f}%) - load shedding",
                            )

                    return (True, None)

                # Normal - reset the failure count
                self._failure_count = 0
                return (True, None)

            if current_state == self.OPEN:
                # Blocking state - check the recovery timeout
                if self._open_time:
                    elapsed = time.time() - self._open_time
                    if elapsed >= self._recovery_timeout:
                        # Recovery timeout elapsed - transition to HALF_OPEN
                        self._set_state(self.HALF_OPEN)
                        self._half_open_requests = 0
                        self._success_count = 0
                        # Allow the first request
                        self._half_open_requests += 1
                        return (True, "Testing recovery (HALF_OPEN)")

                # Still blocking
                self._stats["rejected_requests"] += 1
                remaining = self._recovery_timeout - (
                    time.time() - (self._open_time or 0)
                )
                return (False, f"Circuit OPEN - retry in {remaining:.1f}s")

            if current_state == self.HALF_OPEN:
                # Recovery testing in progress
                if self._half_open_requests < self._half_open_max_requests:
                    self._half_open_requests += 1
                    return (True, "Testing recovery (HALF_OPEN)")
                # Test request count exceeded - wait
                self._stats["rejected_requests"] += 1
                return (False, "HALF_OPEN test in progress - wait")

        return (True, None)

    def record_success(self):
        """Record a successful request."""
        with self._state_lock:
            if self._state == self.HALF_OPEN:
                self._success_count += 1
                logger.info(
                    "pool_circuit_breaker.success",
                    success_count=self._success_count,
                    success_threshold=self._success_threshold,
                )

                if self._success_count >= self._success_threshold:
                    # Enough successes - recovery complete!
                    self._set_state(self.CLOSED)
                    self._failure_count = 0
                    self._success_count = 0
                    logger.info("pool_circuit_breaker.recovered_circuit_closed")

            elif self._state == self.CLOSED:
                # Success in normal state - reset the failure counter
                self._failure_count = 0

    def record_failure(self):
        """Record a failed request."""
        with self._state_lock:
            self._last_failure_time = time.time()

            if self._state == self.HALF_OPEN:
                # Recovery test failed - back to OPEN
                self._set_state(self.OPEN)
                logger.warning("pool_circuit_breaker.recovery_failed")

            elif self._state == self.CLOSED:
                self._failure_count += 1
                if self._failure_count >= self._failure_threshold:
                    self._set_state(self.OPEN)

    def get_stats(self) -> dict:
        """Return statistics (including cache statistics)."""
        return {
            "state": self._state,
            "failure_count": self._failure_count,
            "success_count": self._success_count,
            "stats": self._stats.copy(),
            "pool_status": self.get_cached_pool_status(),  # v6.2.0: uses cache
            "cache_interval_ms": self._cache_interval_ms,  # v6.2.0: cache setting info
        }

    def reset(self):
        """Reset state (for testing)."""
        with self._state_lock:
            self._state = self.CLOSED
            self._failure_count = 0
            self._success_count = 0
            self._open_time = None
            self._half_open_requests = 0
            # v6.2.0: also reset cache statistics
            self._stats["cache_hits"] = 0
            self._stats["cache_refreshes"] = 0
            logger.info("pool_circuit_breaker.reset_closed")

    @classmethod
    def reset_instance(cls) -> None:
        """Reset singleton instance for test isolation."""
        with cls._lock:
            instance = cls._instance
            if instance is not None:
                instance._stop_background_refresh()
            cls._instance = None


# Global instance
pool_circuit_breaker = PoolCircuitBreaker()


class PoolCircuitBreakerMiddleware:
    """
    Django Middleware: returns 503 immediately when the pool is exhausted.

    Usage:
    MIDDLEWARE = [
        ...
        'baldur.api.django.pool_circuit_breaker.PoolCircuitBreakerMiddleware',
        ...
    ]

    Disable:
    BALDUR_POOL_CB_MIDDLEWARE_ENABLED = False (settings.py)
    or
    BALDUR_POOL_CB_MIDDLEWARE_ENABLED=false (environment variable)
    """

    # Paths excluded from Circuit Breaker
    EXCLUDED_PATHS = [
        "/health/",
        "/api/baldur/health/",
        "/api/baldur/circuit-breaker/",  # Exclude the CB management API
        "/admin/",
        "/static/",
        "/media/",
    ]

    def __init__(self, get_response):
        self.get_response = get_response
        self._request_count = 0
        # v6.2.0: log pool status every 100 requests (50 -> 100)
        self._log_interval = 100
        self._audit_enabled = self._check_audit_available()
        self._enabled = self._check_enabled()

        # Start the background pool-status refresh thread here — at Django
        # middleware construction (app ready / first request), not at module
        # import. Only the enabled middleware (the sole consumer of the cached
        # status) needs it, so importing baldur.api.django stays side-effect-free
        # for framework-free / non-Django processes. Idempotent: a no-op if the
        # thread is already alive.
        if self._enabled:
            pool_circuit_breaker._start_background_refresh()

        status = "enabled" if self._enabled else "DISABLED"
        logger.info(
            "pool_circuit_breaker_middleware.initialized_audit",
            cb_status=status,
            audit_enabled="enabled" if self._audit_enabled else "disabled",
        )

    def _check_enabled(self) -> bool:
        """Check whether the middleware is enabled."""
        try:
            from django.conf import settings

            return getattr(settings, "BALDUR_POOL_CB_MIDDLEWARE_ENABLED", True)
        except Exception:
            # Check the environment variable when settings are inaccessible
            return os.getenv("BALDUR_POOL_CB_MIDDLEWARE_ENABLED", "true").lower() in (
                "true",
                "1",
                "yes",
            )

    def _check_audit_available(self) -> bool:
        """Check whether the audit system is available."""
        try:
            from baldur.audit import ContinuousAuditRecorder  # noqa: F401

            return True
        except ImportError:
            return False

    def _record_rejection_audit(
        self,
        request,
        reason: str,
        circuit_state: str,
        pool_status: dict,
    ):
        """
        v6.2.1: record an audit log on a 503 rejection.

        Clearly records that this is a cache-based decision to avoid confusion during analysis.

        - Prefers the RequestAuditBuffer pattern (batch-recorded by AuditMiddleware)
        - Falls back to a direct ContinuousAuditRecorder call when the buffer is unavailable
        """
        if not self._audit_enabled:
            return

        # === Prefer the audit buffer pattern ===
        try:
            from baldur.audit.event_buffer import (
                AuditEventType,
                RequestAuditBuffer,
            )

            # Extract cache metadata
            cache_age_ms = pool_status.get("_cache_age_ms", 0)
            is_stale = pool_status.get("_is_stale", False)
            is_stale_fallback = pool_status.get("_stale_fallback", False)

            buffer = RequestAuditBuffer.get_or_create(request)
            buffer.add(
                event_type=AuditEventType.POOL_CB_REJECTION,
                source="PoolCircuitBreakerMiddleware",
                details={
                    "request_path": request.path,
                    "request_method": request.method,
                    "circuit_state": circuit_state,
                    "rejection_reason": reason,
                    "decision_source": "cached_pool_status",
                    "cache_age_ms": cache_age_ms,
                    "is_stale": is_stale,
                    "is_stale_fallback": is_stale_fallback,
                    "pool_checkedout": pool_status.get("checkedout"),
                    "pool_total_capacity": pool_status.get("total_capacity"),
                    "pool_usage_percent": pool_status.get("usage_percent"),
                    "pool_is_exhausted": pool_status.get("is_exhausted"),
                },
                success=False,
                error_message=reason,
            )
            return  # Added to the buffer - recorded by AuditMiddleware
        except ImportError:
            pass  # event_buffer unavailable - fallback

        # === Fallback: structured warning log when event_buffer is unavailable ===
        # The previous fallback called ``ContinuousAuditRecorder.record(...)``
        # via an ``AuditActionType.CIRCUIT_BREAKER_TRIGGERED`` enum member that
        # never existed (recorder exposes ``record_compliance_check`` /
        # ``record_drift_detected`` etc., not a generic ``record``). Since this
        # branch is only hit when the primary buffer path is unavailable and
        # the prior try/except swallowed every error, the audit value here was
        # zero. Emit a structured WARNING so SRE still sees the rejection.
        cache_age_ms = pool_status.get("_cache_age_ms", 0)
        is_stale = pool_status.get("_is_stale", False)
        is_stale_fallback = pool_status.get("_stale_fallback", False)
        logger.warning(
            "pool_circuit_breaker_middleware.request_rejected_audit_fallback",
            request_path=request.path,
            request_method=request.method,
            circuit_state=circuit_state,
            rejection_reason=reason,
            decision_source="cached_pool_status",
            cache_age_ms=cache_age_ms,
            is_stale=is_stale,
            is_stale_fallback=is_stale_fallback,
            pool_checkedout=pool_status.get("checkedout"),
            pool_total_capacity=pool_status.get("total_capacity"),
            pool_usage_percent=pool_status.get("usage_percent"),
            pool_is_exhausted=pool_status.get("is_exhausted"),
        )

    def __call__(self, request):  # noqa: C901, PLR0912
        # Bypass when the middleware is disabled
        if not self._enabled:
            return self.get_response(request)

        # Check excluded paths
        path = request.path
        for excluded in self.EXCLUDED_PATHS:
            if path.startswith(excluded):
                return self.get_response(request)

        # Periodic pool-status logging (uses the cached value)
        self._request_count += 1
        if self._request_count % self._log_interval == 0:
            # v6.2.0: uses the cached pool status (Non-Blocking)
            pool_status = pool_circuit_breaker.get_cached_pool_status()
            cache_stats = pool_circuit_breaker._stats
            logger.info(
                "pool_circuit_breaker_middleware.pool_status_every_reqs",
                log_interval=self._log_interval,
                pool_status=pool_status.get("checkedout", "?"),
                total_capacity=pool_status.get("total_capacity", "?"),
                usage_percent=pool_status.get("usage_percent", 0),
                is_exhausted=pool_status.get("is_exhausted", False),
                cache_stats=cache_stats.get("cache_hits", 0),
            )

        cb = pool_circuit_breaker
        allow, reason = cb.should_allow_request()

        if not allow:
            # Reject immediately! (Fail Fast)
            logger.warning(
                "pool_circuit_breaker_middleware.rejected",
                path=path,
                reason=reason,
            )

            # v6.2.1: audit integration - note this is a cache-based decision
            pool_status = cb.get_cached_pool_status()
            self._record_rejection_audit(
                request=request,
                reason=reason,
                circuit_state=cb.state,
                pool_status=pool_status,
            )

            return JsonResponse(
                {
                    "error": "Service temporarily unavailable",
                    "reason": reason,
                    "circuit_state": cb.state,
                    "retry_after": cb._recovery_timeout,
                    # v6.2.1: cache metadata (for debugging/analysis)
                    "_cache_based_decision": True,
                    "_cache_age_ms": pool_status.get("_cache_age_ms", 0),
                    "_is_stale": pool_status.get("_is_stale", False),
                },
                status=503,
                headers={"Retry-After": str(cb._recovery_timeout)},
            )

        # Handle the request
        try:
            response = self.get_response(request)

            # Determine success
            if response.status_code < 500:
                cb.record_success()
            else:
                # 500 error - check for pool exhaustion (uses the cached value)
                try:
                    # v6.2.0: check the cached pool status (Non-Blocking)
                    pool_status = cb.get_cached_pool_status()
                    if pool_status.get("is_exhausted", False):
                        # Pool exhausted! OPEN immediately
                        logger.error(
                            "pool_circuit_breaker_middleware.error_pool_exhaustion_path",
                            path=path,
                            pool_status=pool_status,
                        )
                        # Reach the threshold immediately
                        cb._failure_count = cb._failure_threshold
                        cb.record_failure()
                    else:
                        cb.record_failure()
                except Exception:
                    cb.record_failure()

            return response

        except Exception as e:
            # Detect pool timeout or DB connection error
            error_str = str(e).lower()

            # Pool-exhaustion-related exception patterns
            pool_exhaustion_patterns = [
                "timeout",
                "queuepool limit",
                "pool exhausted",
                "connection pool",
                "too many connections",
                "can't get connection",
                "no connections available",
            ]

            is_pool_exhaustion = any(p in error_str for p in pool_exhaustion_patterns)

            if is_pool_exhaustion:
                # Pool exhaustion exception! Transition to OPEN immediately
                logger.exception(
                    "pool_circuit_breaker_middleware.pool_exhaustion_detected",
                    error=e,
                )
                # Reach the threshold immediately
                cb._failure_count = cb._failure_threshold
                cb.record_failure()

                # v6.2.0: log the cached pool status (Non-Blocking)
                pool_status = cb.get_cached_pool_status()
                logger.exception(
                    "pool_circuit_breaker_middleware.pool_status_exhaustion",
                    pool_status=pool_status.get("checkedout", "?"),
                    total_capacity=pool_status.get("total_capacity", "?"),
                    overflow=pool_status.get("overflow", "?"),
                )

                # Return 503 to drive retries. The underlying exception text is
                # recorded via logger.exception above; it is deliberately NOT
                # echoed in the client response to avoid leaking database/driver
                # internals (stack-trace exposure) to callers.
                return JsonResponse(
                    {
                        "error": "Database pool exhausted",
                        "reason": "database connection pool exhausted",
                        "circuit_state": cb.state,
                        "retry_after": cb._recovery_timeout,
                    },
                    status=503,
                    headers={"Retry-After": str(cb._recovery_timeout)},
                )
            # General error
            cb.record_failure()
            logger.exception(
                "pool_circuit_breaker_middleware.request_failed",
                error=e,
            )
            raise


# Circuit Breaker status API
def circuit_breaker_status(request):
    """Circuit Breaker status API.

    v6.1.0: also includes the CircuitBreakerService state from BaldurMiddleware.
    """
    cb = pool_circuit_breaker
    stats = cb.get_stats()

    # v6.1.0: also look up the CircuitBreakerService state (used by BaldurMiddleware)
    cb_service_state = "unknown"
    cb_service_failure_count = 0
    try:
        from baldur.services.circuit_breaker.convenience import (
            get_circuit_breaker_service,
        )

        cb_service = get_circuit_breaker_service()
        if cb_service and cb_service.is_enabled:
            state_data = cb_service.get_or_create_state("database")
            cb_service_state = (
                state_data.state if hasattr(state_data, "state") else str(state_data)
            )
            cb_service_failure_count = getattr(state_data, "failure_count", 0)
    except Exception as e:
        logger.debug(
            "circuit_breaker_status.cb_service_state_lookup",
            error=e,
        )

    # If either CB is open, report the non-closed state
    combined_state = stats["state"]
    if cb_service_state in (
        CircuitBreakerStateEnum.OPEN.value,
        CircuitBreakerStateEnum.HALF_OPEN.value,
    ):
        combined_state = cb_service_state

    return JsonResponse(
        {
            "circuit_breaker": {
                "state": combined_state,  # v6.1.0: combined state
                "failure_count": max(stats["failure_count"], cb_service_failure_count),
                "success_count": stats["success_count"],
                "failure_threshold": cb._failure_threshold,
                "success_threshold": cb._success_threshold,
                "recovery_timeout_seconds": cb._recovery_timeout,
            },
            "pool_circuit_breaker": {
                "state": stats["state"],
                "failure_count": stats["failure_count"],
            },
            "service_circuit_breaker": {
                "state": cb_service_state,
                "failure_count": cb_service_failure_count,
                "service_name": "database",
            },
            "pool": stats["pool_status"],
            "statistics": stats["stats"],
        }
    )


def circuit_breaker_reset(request):
    """Circuit Breaker reset API (for administration)."""
    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)

    cb = pool_circuit_breaker
    cb.reset()

    return JsonResponse(
        {
            "message": "Circuit Breaker reset to closed",
            "state": cb.state,
        }
    )
