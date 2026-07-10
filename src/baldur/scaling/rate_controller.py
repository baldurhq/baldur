"""
Rate-aware Backpressure Controller.

Dynamically adjusts throughput to prevent overload.
Applies the AIMD (Additive Increase, Multiplicative Decrease) pattern.

Note:
    This module is for synchronous (threading) environments.
    It may block the event loop under asyncio.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog

from baldur.core.rate_limiting import TokenBucket
from baldur.scaling.config import (
    BackpressureLevel,
    BackpressureSettings,
    BackpressureStrategy,
    get_backpressure_settings,
)

if TYPE_CHECKING:
    from baldur.meta.daemon_worker import (  # noqa: F401
        DaemonWorkerHandle,
    )
    from baldur.scaling.metrics import BackpressureMetrics

logger = structlog.get_logger()


# Per-priority token-ratio threshold (watermark).
# When the current token-remaining ratio is below this value, requests of that
# priority are rejected.
# Backward compatibility: keep the existing import. Dynamic changes are done via
# BackpressureSettings fields. should_process() reads from settings on every call
# to reflect dynamic changes.
PRIORITY_WATERMARKS: dict[str, float] = {
    "critical": 0.0,
    "standard": 0.3,
    "non_essential": 0.6,
}


@dataclass
class RateControllerState:
    """Current Rate Controller state."""

    current_rate: float
    """Current throughput (items/second)."""

    target_rate: float
    """Target throughput."""

    level: BackpressureLevel
    """Backpressure level."""

    queue_size: int
    """Current queue size."""

    processed_count: int
    """Number of processed items."""

    dropped_count: int
    """Number of dropped items."""

    dropped_by_tier: dict[str, int] | None = None
    """Dropped item count per tier (critical / standard / non_essential)."""

    processed_by_tier: dict[str, int] | None = None
    """Processed item count per tier (critical / standard / non_essential)."""


# TokenBucket has moved to baldur.core.rate_limiting (shared primitives home)
# and is re-exported at module import above so this module's public name and
# scaling.__all__ stay stable. New consumers import from the primitives home.


# Starvation Relief configuration constants
STARVATION_RELIEF_SECONDS = 300.0
"""Relax the watermark after this many seconds of continuous rejection. Default 5 minutes."""

STARVATION_RELIEF_WATERMARK = 0.3
"""Watermark applied when relaxing (same level as the standard tier)."""


class RateController:
    """
    Rate-aware Backpressure Controller.

    Features:
    - Queue-size-based backpressure level calculation
    - Dynamic rate adjustment (AIMD pattern)
    - Strategy-based handling (Throttle, Drop, Reject)

    Usage:
        controller = RateController()
        controller.start()

        if controller.should_process():
            process_item()
        else:
            # Backpressure is active
            pass

        controller.stop()
    """

    def __init__(
        self,
        settings: BackpressureSettings | None = None,
        queue_size_provider: Callable[[], int] | None = None,
        metrics: BackpressureMetrics | None = None,
    ):
        """
        Args:
            settings: Backpressure settings
            queue_size_provider: Queue-size provider function
            metrics: Prometheus per-tier metrics instance (None -> Prometheus not emitted)
        """
        self._settings = settings or get_backpressure_settings()
        self._queue_size_provider = queue_size_provider or (lambda: 0)
        self._metrics = metrics

        self._lock = threading.RLock()
        self._current_rate = self._settings.max_rate_per_second
        self._level = BackpressureLevel.NONE
        self._token_bucket = TokenBucket(self._current_rate)

        # Statistics
        self._processed_count = 0
        self._dropped_count = 0
        self._dropped_by_tier: dict[str, int] = {
            "critical": 0,
            "standard": 0,
            "non_essential": 0,
        }
        self._processed_by_tier: dict[str, int] = {
            "critical": 0,
            "standard": 0,
            "non_essential": 0,
        }

        # Background adjustment thread
        self._running = False
        self._worker: threading.Thread | None = None
        self._handle: DaemonWorkerHandle | None = None  # impl 489 D9

        # Redis sync tracking (for multi-process LS stats)
        self._last_flushed_processed = 0
        self._last_flushed_dropped = 0
        self._last_flushed_by_tier: dict[str, int] = {}

        # Starvation Relief: last-allowed time per tier (monotonic)
        self._tier_last_allowed: dict[str, float] = {
            "critical": time.monotonic(),
            "standard": time.monotonic(),
            "non_essential": time.monotonic(),
        }

        # External level from Throttle SLA (PX4 bridge)
        self._external_level: BackpressureLevel = BackpressureLevel.NONE
        self._external_level_until: float = 0.0

        # EventBus subscription tracking
        self._sla_subscribed: bool = False

    def get_state(self) -> RateControllerState:
        """Return the current state."""
        with self._lock:
            return RateControllerState(
                current_rate=self._current_rate,
                target_rate=self._settings.max_rate_per_second,
                level=self._level,
                queue_size=self._queue_size_provider(),
                processed_count=self._processed_count,
                dropped_count=self._dropped_count,
                dropped_by_tier=dict(self._dropped_by_tier),
                processed_by_tier=dict(self._processed_by_tier),
            )

    def should_process(self, priority: str = "standard") -> bool:  # noqa: C901, PLR0912
        """
        Decide whether to process (priority-based watermark applied).

        When the current token-remaining ratio is below the per-priority
        watermark, the request is rejected immediately without attempting token
        consumption. This prioritizes protecting critical requests when tokens
        are scarce.

        Args:
            priority: Request priority tier.
                "critical" | "standard" | "non_essential".
                The default "standard" is backward compatible with existing callers.

        Returns:
            True to process, False to reject via backpressure
        """
        if not self._settings.backpressure_enabled:
            return True

        # Watermark check: read dynamically from settings to reflect runtime changes
        watermarks = self._settings.get_priority_watermarks()
        watermark = watermarks.get(priority, 0.3)
        token_ratio = self._token_bucket.get_token_ratio()

        if token_ratio < watermark:
            # Starvation Relief: temporarily relax the watermark for a tier
            # rejected continuously for N minutes
            relief_applied = False
            if priority in self._tier_last_allowed:
                elapsed = time.monotonic() - self._tier_last_allowed[priority]
                if (
                    elapsed > STARVATION_RELIEF_SECONDS
                    and self._check_starvation_relief_allowed()
                ):
                    watermark = min(watermark, STARVATION_RELIEF_WATERMARK)
                    logger.warning(
                        "rate_controller.starvation_relief_applied",
                        tier=priority,
                        elapsed_seconds=elapsed,
                        relaxed_watermark=watermark,
                    )
                    relief_applied = True

            if (not relief_applied or token_ratio < watermark) and not relief_applied:
                self._record_drop(priority)
                logger.debug(
                    "rate_controller.request_rejected",
                    tier=priority,
                    reason="watermark_exceeded",
                    token_ratio=token_ratio,
                    watermark=watermark,
                )
                return False

        # Attempt token consumption from the token bucket (single bucket)
        if self._token_bucket.consume():
            self._record_process(priority)
            return True

        # Handle per strategy when tokens are insufficient
        logger.debug(
            "rate_controller.request_rejected",
            tier=priority,
            reason="token_exhausted",
            token_ratio=token_ratio,
        )
        strategy = self._settings.default_strategy

        if strategy == BackpressureStrategy.REJECT:
            self._record_drop(priority)
            return False

        if strategy == BackpressureStrategy.THROTTLE:
            # Wait briefly and retry
            if self._token_bucket.wait_for_token(timeout=0.1):
                self._record_process(priority)
                return True
            self._record_drop(priority)
            return False

        if strategy == BackpressureStrategy.DROP_OLDEST:
            # DROP_OLDEST is handled by the caller
            return True

        if strategy == BackpressureStrategy.QUEUE:
            # QUEUE is handled by the caller
            return True

        return True

    def _record_drop(self, priority: str) -> None:
        """Record a dropped request: increment counters and fire metric.

        Counter and metric only — caller is responsible for logging because
        the log reason ("watermark_exceeded" vs "token_exhausted") and the
        accompanying extras differ per call site.
        """
        with self._lock:
            self._dropped_count += 1
            if priority in self._dropped_by_tier:
                self._dropped_by_tier[priority] += 1
        if self._metrics is not None:
            self._metrics.inc_dropped_by_tier(priority)

    def _record_process(self, priority: str) -> None:
        """Record a processed request: increment counters, update last_allowed, fire metric."""
        with self._lock:
            self._processed_count += 1
            if priority in self._processed_by_tier:
                self._processed_by_tier[priority] += 1
            self._tier_last_allowed[priority] = time.monotonic()
        if self._metrics is not None:
            self._metrics.inc_processed_by_tier(priority)

    def _check_starvation_relief_allowed(self) -> bool:
        """Check system stability before activating Starvation Relief.

        Uses the same criteria as RecoveryGate (CPU < 80%, error_rate < 5%) to
        prevent Relief from increasing traffic while overloaded.

        Returns:
            True to allow Relief, False to block it
        """
        try:
            from baldur_pro.services.emergency_mode.recovery_gate import (
                RecoveryGate,
            )

            gate = RecoveryGate()
            allowed, reason = gate.check_recovery_allowed()
            if not allowed:
                logger.info(
                    "rate_controller.starvation_relief_blocked",
                    reason=reason,
                )
            return allowed
        except Exception:
            # Safely block Relief when RecoveryGate is unavailable
            return False

    def _get_resource_pressure_multiplier(self) -> float:
        """CPU-utilization-based rate attenuation multiplier.

        Reads the cached CPU utilization from SystemMetricsCache and determines
        the rate multiplier by threshold.
        The cache read is ~0ms (lock-free, GIL-atomic reference swap).

        Returns:
            1.0 (normal), 0.5 (CPU >= high_threshold), 0.1 (CPU >= critical_threshold)
        """
        try:
            from baldur.services.system_metrics_cache import (
                get_cached_cpu_percent,
            )

            cpu = get_cached_cpu_percent()
            if cpu >= self._settings.resource_cpu_critical_threshold:
                return 0.1
            if cpu >= self._settings.resource_cpu_high_threshold:
                return 0.5
        except Exception:
            pass
        return 1.0

    def _adjust_rate(self) -> None:
        """
        Rate adjustment (AIMD pattern).

        - Under overload: level-differentiated decrease (Multiplicative Decrease)
        - On normalization: gradual increase (Additive Increase)
        """
        queue_size = self._queue_size_provider()
        queue_level = self._settings.get_level_for_queue_size(queue_size)

        # Expire external level TTL
        with self._lock:
            if (
                self._external_level != BackpressureLevel.NONE
                and time.time() > self._external_level_until
            ):
                self._external_level = BackpressureLevel.NONE

            # max(queue, external) — conservative policy
            new_level = max(queue_level, self._external_level)
            self._level = new_level

        # AIMD pattern: apply the per-level rate multiplier
        if new_level == BackpressureLevel.NONE:
            # Normal: gradual increase (Additive Increase)
            new_rate = self._current_rate * self._settings.rate_increase_factor
        else:
            # Overload: level-differentiated decrease (Multiplicative Decrease)
            multiplier = self._settings.get_rate_multiplier(new_level)
            new_rate = self._settings.max_rate_per_second * multiplier

        # Apply additional CPU-utilization-based attenuation
        resource_multiplier = self._get_resource_pressure_multiplier()
        new_rate *= resource_multiplier

        # Clamp to range
        new_rate = max(
            self._settings.min_rate_per_second,
            min(self._settings.max_rate_per_second, new_rate),
        )

        with self._lock:
            if new_rate != self._current_rate:
                self._current_rate = new_rate
                self._token_bucket.set_rate(new_rate)
                logger.info(
                    "rate_controller.rate_adjusted",
                    new_rate=new_rate,
                    new_level=new_level.value,
                    queue_size=queue_size,
                    rate_multiplier=self._settings.get_rate_multiplier(new_level),
                )

    def _flush_to_redis(self) -> None:
        """Flush counter deltas to Redis for multi-process aggregation."""
        try:
            from datetime import timedelta

            from baldur.factory import ProviderRegistry
            from baldur.utils.time import utc_now

            cache = ProviderRegistry.get_cache()
            date_key = utc_now().strftime("%Y-%m-%d")
            with self._lock:
                d_processed = self._processed_count - self._last_flushed_processed
                d_dropped = self._dropped_count - self._last_flushed_dropped
                d_by_tier = {
                    t: self._dropped_by_tier[t] - self._last_flushed_by_tier.get(t, 0)
                    for t in self._dropped_by_tier
                }
                self._last_flushed_processed = self._processed_count
                self._last_flushed_dropped = self._dropped_count
                self._last_flushed_by_tier = dict(self._dropped_by_tier)
            prefix = f"baldur:rate_controller:{date_key}"
            ttl = timedelta(hours=48)
            if d_processed > 0:
                cache.incr(f"{prefix}:processed", d_processed)
                cache.expire(f"{prefix}:processed", ttl)
            if d_dropped > 0:
                cache.incr(f"{prefix}:dropped", d_dropped)
                cache.expire(f"{prefix}:dropped", ttl)
            for tier, delta in d_by_tier.items():
                if delta > 0:
                    cache.incr(f"{prefix}:dropped:{tier}", delta)
                    cache.expire(f"{prefix}:dropped:{tier}", ttl)
        except Exception:
            pass  # fail-open — Redis down does not affect rate limiting

    def _run_loop(self) -> None:
        """Background rate adjustment loop."""
        while self._running:
            iter_start = time.monotonic()
            try:
                self._adjust_rate()
            except Exception as e:
                logger.exception(
                    "rate_controller.adjust_error",
                    error=e,
                )

            if self._settings.redis_sync_enabled:
                self._flush_to_redis()

            if self._handle is not None:
                self._handle.observe_iteration(time.monotonic() - iter_start)
                self._handle.heartbeat()

            time.sleep(self._settings.rate_adjust_interval_seconds)

    def _run_loop_with_crash_capture(self) -> None:
        try:
            self._run_loop()
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as e:
            if self._handle is not None:
                self._handle.record_crash(e)
            raise

    def start(self) -> None:
        """Start rate adjustment."""
        from baldur.meta.daemon_worker import DaemonWorkerHandle
        from baldur.metrics.recorders.daemon_worker import register_daemon_worker

        if not self._settings.backpressure_enabled:
            logger.info("rate_controller.disabled")
            return

        if self._running:
            return

        self._subscribe_throttle_sla_events()

        self._running = True
        self._spawn_worker_thread()
        assert self._worker is not None  # populated by _spawn_worker_thread
        self._handle = DaemonWorkerHandle(
            thread=self._worker,
            tick_interval_seconds=self._settings.rate_adjust_interval_seconds,
            restart_callback=self._spawn_worker_thread,
        )
        register_daemon_worker("RateController", self._handle)
        logger.info("rate_controller.started")

    def _spawn_worker_thread(self) -> None:
        """Construct + start a fresh rate-adjust thread (impl 489 D9)."""
        self._worker = threading.Thread(
            target=self._run_loop_with_crash_capture,
            name="RateController",
            daemon=True,
        )
        self._worker.start()
        if self._handle is not None:
            self._handle.thread = self._worker

    def _subscribe_throttle_sla_events(self) -> None:
        """Subscribe to THROTTLE_SLA_CRITICAL for external level bridge (Fail-Open).

        Skipped when backpressure_enabled=False — no need for external signals
        when backpressure is disabled.
        """
        if not self._settings.backpressure_enabled:
            return

        if self._sla_subscribed:
            return

        try:
            from baldur.services.event_bus import EventType, get_event_bus

            bus = get_event_bus()
            bus.subscribe(
                EventType.THROTTLE_SLA_CRITICAL,
                self._handle_throttle_sla_critical,
            )
            self._sla_subscribed = True
            logger.info("rate_controller.subscribed_throttle_sla_events")
        except ImportError:
            logger.debug("rate_controller.eventbus_unavailable")
        except Exception as e:
            logger.warning(
                "rate_controller.subscribe_throttle_sla_failed",
                error=e,
            )

    def _handle_throttle_sla_critical(self, event) -> None:
        """Set external backpressure level on Throttle SLA critical event.

        Fixed at BackpressureLevel.HIGH — Throttle's reduction_percent is always
        30% hardcoded, so dynamic mapping would be dead code. Each event reception
        renews the TTL (lease pattern).
        """
        with self._lock:
            self._external_level = BackpressureLevel.HIGH
            self._external_level_until = (
                time.time() + self._settings.external_level_ttl_seconds
            )

    def stop(self) -> None:
        """Stop rate adjustment and unsubscribe from the EventBus."""
        # Unsubscribe EventBus handlers first
        if self._sla_subscribed:
            try:
                from baldur.services.event_bus import EventType, get_event_bus

                bus = get_event_bus()
                bus.unsubscribe(
                    EventType.THROTTLE_SLA_CRITICAL,
                    self._handle_throttle_sla_critical,
                )
                self._sla_subscribed = False
                logger.debug("rate_controller.unsubscribed_throttle_sla_events")
            except ImportError:
                pass
            except Exception:
                self._sla_subscribed = False

        from baldur.metrics.recorders.daemon_worker import unregister_daemon_worker
        from baldur.settings.thread_management import (
            get_thread_management_settings,
        )

        if self._handle is not None:
            self._handle.is_stopping = True
        self._running = False
        timeout = get_thread_management_settings().join_timeout
        if self._worker:
            self._worker.join(timeout=timeout)
        unregister_daemon_worker("RateController")
        if self._worker is not None and self._worker.is_alive():
            logger.critical(
                "daemon_worker.stop_join_timeout",
                worker_name="RateController",
                join_timeout_seconds=timeout,
            )
        logger.info("rate_controller.stopped")


# =============================================================================
# Singleton
# =============================================================================

from baldur.utils.singleton import CLEANUP_STOP, make_singleton_factory


def _create_rate_controller() -> RateController:
    from baldur.scaling.metrics import get_backpressure_metrics

    return RateController(metrics=get_backpressure_metrics())


get_rate_controller, configure_rate_controller, reset_rate_controller = (
    make_singleton_factory(
        "rate_controller", _create_rate_controller, cleanup_fn=CLEANUP_STOP
    )
)
