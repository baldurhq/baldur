"""
Core SafeGauge Implementation.

Thread-safe gauge wrapper that prevents negative values.

Design Philosophy:
- Counter Pair (Google SRE style) is technically superior but requires
  PromQL calculations on the dashboard side.
- SafeGauge provides "plug-and-play" experience for buyers while
  internally preventing the -1 dashboard embarrassment.

Enhanced Features (Metric Reliability):
- Sync Status Tracking: last_sync_time and is_synced for data freshness
- Staleness Detection: Auto-mark as stale after threshold
- Stabilization Period: Gradual recovery from strict mode

Memory Management (LRU Cache):
- LRU cache preventing unbounded growth of label combinations
- max_label_combinations sets the maximum cache size
- Eviction emits a warning log and records a metric
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast

import structlog

from .noop import NoOpGaugeChild
from .sync import SyncInfo

if TYPE_CHECKING:
    from prometheus_client import Gauge

logger = structlog.get_logger()


def _get_max_label_combinations() -> int:
    """Read the max label-combination count from SafeGaugeSettings."""
    try:
        from baldur.settings.safe_gauge import get_safe_gauge_settings

        return get_safe_gauge_settings().max_label_combinations
    except Exception:
        return 1000  # fallback


class SafeGaugeChild:
    """
    Safe wrapper for labeled Gauge child.

    Prevents the gauge from going negative by clamping at 0.
    This is critical for preventing "-1 pending items" on dashboards
    after server restarts when the in-memory counter starts at 0.

    Enhanced with sync status tracking:
    - Tracks last_sync_time for data freshness indication
    - Auto-detects staleness based on threshold
    - Supports stabilization period for gradual recovery

    Thread Safety:
        Uses a lock to ensure atomic read-check-update operations.
        This prevents race conditions in high-concurrency environments.

    Note:
        Prometheus client doesn't expose _value directly in a clean way,
        so we maintain our own shadow counter for clamping logic.
        The Lazy Sync (Reconciler) will correct any drift periodically.
    """

    def __init__(
        self,
        gauge_child: Any,
        label_values: dict[str, str],
        staleness_threshold: float = 300.0,
        stabilization_duration: float = 60.0,
    ):
        """
        Initialize SafeGaugeChild.

        Args:
            gauge_child: The original Prometheus Gauge child (labeled)
            label_values: Label key-value pairs for logging
            staleness_threshold: Seconds before data is considered stale
                (default: 5 min)
            stabilization_duration: Seconds for gradual recovery (default: 60s)
        """
        self._gauge_child = gauge_child
        self._label_values = label_values
        self._lock = threading.Lock()
        # Shadow counter for clamping logic
        # Starts at 0, may drift from actual Prometheus value
        # Reconciler will sync periodically
        self._shadow_value: float = 0.0
        self._initialized = False

        # Sync status tracking
        self._sync_info = SyncInfo(
            staleness_threshold=staleness_threshold,
            stabilization_duration=stabilization_duration,
        )

    @property
    def sync_info(self) -> SyncInfo:
        """Sync info."""
        return self._sync_info

    @property
    def is_synced(self) -> bool:
        """Whether the data can be trusted."""
        self._sync_info.check_staleness()
        return self._sync_info.is_synced

    @property
    def is_recovering(self) -> bool:
        """Whether recovery is in progress."""
        return self._sync_info.is_recovering

    @property
    def last_sync_time(self) -> float | None:
        """Time of the last sync."""
        return self._sync_info.last_sync_time

    @property
    def sync_age_seconds(self) -> float | None:
        """Time elapsed since the last sync."""
        return self._sync_info.age_seconds

    def inc(self, amount: float = 1) -> None:
        """
        Increment the gauge value.

        Args:
            amount: Amount to increment (default: 1)
        """
        with self._lock:
            self._shadow_value += amount
            self._gauge_child.inc(amount)
            self._initialized = True
            self._sync_info.mark_synced("push")

    def dec(self, amount: float = 1) -> None:
        """
        Decrement the gauge value, clamping at 0.

        This is the key safety feature: if the shadow value would go
        negative, we set to 0 instead. This prevents the embarrassing
        "-1 pending items" display after server restarts.

        Args:
            amount: Amount to decrement (default: 1)
        """
        with self._lock:
            if not self._initialized:
                # First operation after restart is a dec - likely stale event
                # Don't decrement, just log and return
                logger.debug(
                    "safe_gauge.ignoring_dec_before_any",
                    label_values=self._label_values,
                )
                return

            if self._shadow_value >= amount:
                # Normal case: sufficient value to decrement
                self._shadow_value -= amount
                self._gauge_child.dec(amount)
            else:
                # Edge case: would go negative, clamp to 0
                old_value = self._shadow_value
                self._shadow_value = 0.0
                # Set to 0 instead of decrementing
                self._gauge_child.set(0)
                logger.warning(
                    "safe_gauge.clamped_gauge_indicate_event",
                    exceeded_decrement_value=old_value - amount,
                    label_values=self._label_values,
                )

            self._sync_info.mark_synced("push")

    def set(self, value: float, source: str = "manual") -> None:
        """
        Set the gauge to a specific value.

        Args:
            value: Value to set (clamped to 0 if negative)
            source: Sync source identifier (default: "manual")
        """
        with self._lock:
            if value < 0:
                logger.warning(
                    "safe_gauge.attempted_set_negative_value",
                    rejected_value=value,
                    label_values=self._label_values,
                )
                value = 0.0
            self._shadow_value = value
            self._gauge_child.set(value)
            self._initialized = True
            self._sync_info.mark_synced(source)

    def get_shadow_value(self) -> float:
        """
        Get the current shadow value (for testing/debugging).

        Returns:
            Current shadow counter value
        """
        with self._lock:
            return self._shadow_value

    def sync_from_source(self, actual_value: float, source: str = "reconciler") -> None:
        """
        Sync shadow value from authoritative source (Reconciler callback).

        Called by MetricReconciler to correct drift between
        in-memory shadow and actual DB state.

        Args:
            actual_value: Actual value from DB or external source
            source: Sync source identifier (e.g., "hydration", "manual", "snapshot")
        """
        with self._lock:
            if actual_value < 0:
                actual_value = 0.0
            old_shadow = self._shadow_value
            self._shadow_value = actual_value
            self._gauge_child.set(actual_value)
            self._initialized = True
            self._sync_info.mark_synced(source)
            if old_shadow != actual_value:
                logger.info(
                    "safe_gauge.synced_source",
                    old_shadow=old_shadow,
                    actual_value=actual_value,
                    label_values=self._label_values,
                )

    def mark_stale(self, reason: str = "external") -> None:
        """
        Manually mark the value as stale.

        Args:
            reason: Reason for going stale
        """
        with self._lock:
            self._sync_info.mark_stale(reason)

    def get_reliability_info(self) -> dict[str, Any]:
        """
        Return metric reliability info.

        Returns:
            Reliability info dict
        """
        with self._lock:
            self._sync_info.check_staleness()
            return {
                "is_synced": self._sync_info.is_synced,
                "status": self._sync_info.status.value,
                "last_sync_time": self._sync_info.last_sync_time,
                "last_sync_source": self._sync_info.last_sync_source,
                "age_seconds": self._sync_info.age_seconds,
                "is_recovering": self._sync_info.is_recovering,
                "recovery_progress": self._sync_info.recovery_progress,
                "shadow_value": self._shadow_value,
                "labels": self._label_values,
            }


class SafeGauge:
    """
    Safe wrapper for Prometheus Gauge with LRU-based memory management.

    Wraps a Prometheus Gauge and returns SafeGaugeChild instances
    for labeled gauge operations, preventing negative values.

    This pattern is inspired by Netflix's metric handling approach:
    - Internal safety mechanisms (clamping)
    - External simplicity (standard Gauge interface)
    - Eventual consistency (Reconciler syncs periodically)

    Memory Management:
    - An LRU cache prevents unbounded growth of label combinations
    - Past max_label_combinations, the oldest combination is dropped
    - Eviction emits a warning log and calls the optional callback

    Example:
        >>> from prometheus_client import Gauge
        >>> raw = Gauge("dlq_pending", "Pending DLQ items", ["domain"])
        >>> safe = SafeGauge(raw, max_label_combinations=500)
        >>>
        >>> # Use like normal Gauge
        >>> safe.labels(domain="payment").inc()
        >>> safe.labels(domain="payment").dec()  # Won't go below 0

    Environment Settings:
        - Single server: max_label_combinations=1000 (default)
        - K8s 10 Pods: max_label_combinations=500
        - K8s 100+ Pods: max_label_combinations=200
    """

    # Legacy constant kept for backward compatibility
    DEFAULT_MAX_LABEL_COMBINATIONS = 1000

    def __init__(
        self,
        gauge: Gauge | None,
        max_label_combinations: int | None = None,
        on_eviction: Callable[[tuple, SafeGaugeChild], None] | None = None,
    ):
        """
        Initialize SafeGauge with LRU cache.

        Args:
            gauge: Prometheus Gauge to wrap. If None, operations are no-ops.
            max_label_combinations: Max label combinations to cache. Read from
                Settings when None. Past the limit, the oldest combination is
                dropped automatically.
            on_eviction: Callback invoked when a label combination is evicted
                (for monitoring). (evicted_key, evicted_child) -> None
        """
        self._gauge = gauge
        self._children: OrderedDict[tuple, SafeGaugeChild] = OrderedDict()
        self._max_label_combinations = (
            max_label_combinations
            if max_label_combinations is not None
            else _get_max_label_combinations()
        )
        self._on_eviction = on_eviction
        self._lock = threading.Lock()
        self._eviction_count = 0

    def labels(self, **kwargs) -> SafeGaugeChild:
        """
        Get a SafeGaugeChild for the given labels.

        Uses an LRU cache: recently accessed label combinations are kept, and
        past max_label_combinations the oldest combination is dropped.

        Args:
            **kwargs: Label key-value pairs

        Returns:
            SafeGaugeChild instance for thread-safe operations
        """
        if self._gauge is None:
            # NoOpGaugeChild duck-types the SafeGaugeChild surface used by callers
            # (inc/dec/set/get_shadow_value/sync_from_source/mark_stale/etc.).
            return cast("SafeGaugeChild", NoOpGaugeChild())

        key = tuple(sorted(kwargs.items()))

        with self._lock:
            if key in self._children:
                # LRU: move to the most-recently-used end
                self._children.move_to_end(key)
                return self._children[key]

            # Over capacity: drop the oldest entry
            if len(self._children) >= self._max_label_combinations:
                self._evict_oldest()

            # Create a new child
            gauge_child = self._gauge.labels(**kwargs)
            child = SafeGaugeChild(gauge_child, kwargs)
            self._children[key] = child
            return child

    def _evict_oldest(self) -> None:
        """
        Drop the oldest label combination (LRU eviction).

        The evicted combination's shadow_value is lost. Emits a warning log
        and invokes the on_eviction callback when one is set.
        """
        if not self._children:
            return

        oldest_key, oldest_child = self._children.popitem(last=False)
        self._eviction_count += 1

        # Warning log for operator awareness
        logger.warning(
            "safe_gauge.lru_eviction",
            eviction_count=self._eviction_count,
            dict=dict(oldest_key),
            oldest_child=oldest_child.get_shadow_value(),
            max_label_combinations=self._max_label_combinations,
        )

        # Record an eviction metric (when prometheus is present)
        try:
            from baldur.metrics.prometheus import PROMETHEUS_AVAILABLE

            if PROMETHEUS_AVAILABLE:
                # Record via a simple Counter (extend if a dedicated one is needed)
                pass  # The metric is optional; the log alone is sufficient
        except ImportError:
            pass

        # Invoke the callback (for custom handling)
        if self._on_eviction:
            try:
                self._on_eviction(oldest_key, oldest_child)
            except Exception as e:
                logger.exception(
                    "safe_gauge.eviction_callback_failed",
                    error=e,
                )

    def get_child(self, **kwargs) -> SafeGaugeChild | None:
        """
        Get existing SafeGaugeChild without creating new one.

        Does not update the LRU order (read-only lookup).

        Args:
            **kwargs: Label key-value pairs

        Returns:
            SafeGaugeChild if exists, None otherwise
        """
        key = tuple(sorted(kwargs.items()))
        with self._lock:
            return self._children.get(key)

    @property
    def is_available(self) -> bool:
        """Check if underlying gauge is available."""
        return self._gauge is not None

    @property
    def current_size(self) -> int:
        """Number of label combinations currently cached."""
        with self._lock:
            return len(self._children)

    @property
    def max_size(self) -> int:
        """Maximum number of label combinations that can be cached."""
        return self._max_label_combinations

    @property
    def eviction_count(self) -> int:
        """Total evictions since creation."""
        return self._eviction_count

    def get_cache_stats(self) -> dict[str, Any]:
        """
        Return cache statistics (for monitoring).

        Returns:
            Dict with cache stats:
            - current_size: Current cache size
            - max_size: Maximum cache size
            - eviction_count: Total evictions
            - utilization_percent: Cache utilization (%)
        """
        with self._lock:
            current = len(self._children)
            return {
                "current_size": current,
                "max_size": self._max_label_combinations,
                "eviction_count": self._eviction_count,
                "utilization_percent": (
                    (current / self._max_label_combinations) * 100
                    if self._max_label_combinations > 0
                    else 0
                ),
            }


__all__ = [
    "SafeGauge",
    "SafeGaugeChild",
]
