"""
System Metrics Cache - background cache for psutil CPU/Memory.

Measures psutil CPU/Memory every 1 second on a threading.Timer and caches
the result. Consumers (collect_system_snapshot, ResourceGuard,
CircuitBreakerService) read the cache in ~0ms, allowing non-blocking
system metric queries.

Pattern: same threading.Timer + daemon=True pattern as
PrecomputedCacheWorker (services/precomputed_cache/worker.py).
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any

import structlog

from baldur.utils.time import utc_now

logger = structlog.get_logger()


@dataclass(frozen=True)
class CachedMetrics:
    """
    Cached system metrics snapshot (Immutable).

    Set frozen=True to eliminate concurrency hazards on reads.
    The background thread creates a new instance and swaps the reference
    (Copy-on-Write). Reference swap is atomic under the Python GIL, so
    no Lock is required.
    """

    cpu_percent: float = 0.0
    memory_percent: float = 0.0
    memory_used_mb: float = 0.0
    memory_available_mb: float = 0.0
    measured_at: str = ""  # ISO format timestamp
    source: str = "cache"  # "cache" | "direct" (fallback) | "stale"
    age_seconds: float = 0.0  # Time elapsed since the last refresh


class SystemMetricsCache:
    """
    Background cache for system metrics.

    Operation:
    1. On start(), perform one synchronous initial measurement, then start
       the background daemon thread
    2. Every _refresh_interval seconds, call
       psutil.cpu_percent(interval=_sample_interval) + virtual_memory()
    3. Create a new CachedMetrics instance and atomically swap the
       _cached reference
    4. Consumers read in ~0ms via get_metrics() / get_cpu_percent() /
       get_memory_percent()

    Thread safety:
    - Reference swap of _cached is atomic under the GIL (no Lock needed)
    - start()/stop() are guarded by _lock
    """

    def __init__(
        self,
        refresh_interval: float = 1.0,
        sample_interval: float = 0.1,
        max_age_seconds: float = 5.0,
    ):
        self._refresh_interval = refresh_interval
        self._sample_interval = sample_interval
        self._max_age_seconds = max_age_seconds
        self._timer: threading.Timer | None = None
        self._running = False
        self._lock = threading.Lock()
        self._cached = CachedMetrics()
        self._last_refresh: float = 0.0

    # =========================================================================
    # Lifecycle
    # =========================================================================

    def start(self) -> None:
        """
        Start background refresh.

        Cold-start avoidance: perform the first _do_refresh() synchronously
        once so the cache has an initial value immediately, then start
        Timer scheduling. The ~100ms synchronous cost is paid only once at
        AppConfig.ready() startup.
        """
        with self._lock:
            if self._running:
                return
            self._running = True
            logger.info(
                "system_metrics_cache.starting",
                _self=self._refresh_interval,
                sample_interval=self._sample_interval,
            )

            # Cold-start avoidance: run the first measurement synchronously (~100ms)
            self._do_refresh()

            # Schedule subsequent periodic refreshes
            self._schedule_refresh()

    def stop(self) -> None:
        """Stop background refresh."""
        with self._lock:
            self._running = False
            if self._timer:
                self._timer.cancel()
                self._timer = None
            logger.info("system_metrics_cache.stopped")

    def is_running(self) -> bool:
        """Whether the cache worker is running."""
        return self._running

    # =========================================================================
    # Consumer API (Read - Lock-free, ~0ms)
    # =========================================================================

    def get_metrics(self) -> CachedMetrics:
        """
        Return the cached metrics in full.

        If the cache age exceeds max_age_seconds, mark source="stale".
        """
        cached = self._cached
        age = (
            time.monotonic() - self._last_refresh
            if self._last_refresh > 0
            else float("inf")
        )

        if age > self._max_age_seconds:
            return CachedMetrics(
                cpu_percent=cached.cpu_percent,
                memory_percent=cached.memory_percent,
                memory_used_mb=cached.memory_used_mb,
                memory_available_mb=cached.memory_available_mb,
                measured_at=cached.measured_at,
                source="stale",
                age_seconds=round(age, 1),
            )
        return cached

    def get_cpu_percent(self) -> float:
        """Cached CPU usage percent."""
        return self._cached.cpu_percent

    def get_memory_percent(self) -> float:
        """Cached memory usage percent."""
        return self._cached.memory_percent

    def get_snapshot_dict(self) -> dict[str, Any]:
        """
        Return a collect_system_snapshot()-compatible dictionary.

        Only the cpu/memory portion is read from the cache.
        DB connection count, Error Rate, etc. are not included because they
        are non-blocking.
        """
        m = self._cached
        return {
            "cpu_percent": m.cpu_percent,
            "memory_percent": m.memory_percent,
            "memory_used_mb": m.memory_used_mb,
            "memory_available_mb": m.memory_available_mb,
            "metrics_source": m.source,
            "metrics_measured_at": m.measured_at,
        }

    # =========================================================================
    # Internal - Background Refresh
    # =========================================================================

    def _schedule_refresh(self) -> None:
        """Schedule the next refresh."""
        if not self._running:
            return
        self._timer = threading.Timer(self._refresh_interval, self._do_refresh)
        self._timer.daemon = True
        self._timer.start()

    def _do_refresh(self) -> None:
        """
        Measure CPU/Memory via psutil and refresh the cache.

        psutil.cpu_percent(interval=sample_interval) internally sleeps before
        measuring. It runs only on the daemon thread, so the web thread is
        unaffected. Metric precision: round(val, 1) -- follows the
        ResourceCheckResult.to_response_dict() convention.
        """
        try:
            import psutil

            cpu = psutil.cpu_percent(interval=self._sample_interval)
            memory = psutil.virtual_memory()

            self._cached = CachedMetrics(
                cpu_percent=round(cpu, 1),
                memory_percent=round(memory.percent, 1),
                memory_used_mb=round(memory.used / (1024 * 1024), 1),
                memory_available_mb=round(memory.available / (1024 * 1024), 1),
                measured_at=utc_now().isoformat(),
                source="cache",
                age_seconds=0.0,
            )
            self._last_refresh = time.monotonic()

        except Exception as e:
            logger.warning(
                "system_metrics_cache.refresh_failed",
                error=e,
            )

        # Schedule the next refresh
        self._schedule_refresh()

    def get_stats(self) -> dict[str, Any]:
        """Statistics for debugging/monitoring."""
        age = time.monotonic() - self._last_refresh if self._last_refresh > 0 else -1
        return {
            "running": self._running,
            "refresh_interval": self._refresh_interval,
            "sample_interval": self._sample_interval,
            "max_age_seconds": self._max_age_seconds,
            "cache_age_seconds": round(age, 1) if age >= 0 else None,
            "current_cpu_percent": self._cached.cpu_percent,
            "current_memory_percent": self._cached.memory_percent,
            "source": self._cached.source,
        }


# =============================================================================
# Global Singleton + Module-level API
# =============================================================================

_cache = SystemMetricsCache()


def get_system_metrics_cache() -> SystemMetricsCache:
    """Return the global SystemMetricsCache instance."""
    return _cache


def start_system_metrics_cache() -> None:
    """Start the cache worker. Called from AppConfig.ready()."""
    _cache.start()


def stop_system_metrics_cache() -> None:
    """Stop the cache worker."""
    _cache.stop()


def get_cached_cpu_percent() -> float:
    """Return the cached CPU usage percent (~0ms)."""
    return _cache.get_cpu_percent()


def get_cached_memory_percent() -> float:
    """Return the cached memory usage percent (~0ms)."""
    return _cache.get_memory_percent()


def reset_system_metrics_cache() -> None:
    """For tests: reset the global instance."""
    global _cache
    _cache.stop()
    _cache = SystemMetricsCache()
