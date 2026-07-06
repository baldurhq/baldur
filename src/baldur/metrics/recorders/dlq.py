"""
DLQ metric recorder — metric definitions and recording for Dead Letter Queue.

Owns all DLQ-related Prometheus metrics. Metrics are created via
get_or_create_* to avoid duplicate registration errors.
"""

from __future__ import annotations

import structlog

from baldur.metrics.recorders.base import BaseMetricRecorder
from baldur.metrics.registry import (
    get_or_create_counter,
    get_or_create_gauge,
)

logger = structlog.get_logger()

__all__ = ["DLQMetricRecorder"]


class DLQMetricRecorder(BaseMetricRecorder):
    """DLQ metric definitions and recording."""

    def __init__(self) -> None:
        self._items_total = get_or_create_counter(
            f"{self.PREFIX}_dlq_items_total",
            "Total DLQ items created",
            ["domain", "failure_type", "is_synthetic"],
        )
        self._pending_gauge = get_or_create_gauge(
            f"{self.PREFIX}_dlq_pending_count",
            "Current pending DLQ items",
            ["domain"],
        )
        self._by_status_gauge = get_or_create_gauge(
            f"{self.PREFIX}_dlq_items_by_status",
            "DLQ items count by status",
            ["status"],
        )
        self._created_total = get_or_create_counter(
            f"{self.PREFIX}_dlq_created_total",
            "Total DLQ items created (for rate calculation)",
            ["domain"],
        )
        self._overflow_total = get_or_create_counter(
            f"{self.PREFIX}_dlq_overflow_total",
            "Total DLQ overflow events",
            ["domain", "strategy"],
        )
        self._evicted_total = get_or_create_counter(
            f"{self.PREFIX}_dlq_evicted_total",
            "Total DLQ items evicted by overflow",
            ["domain", "strategy"],
        )
        self._rejected_total = get_or_create_counter(
            f"{self.PREFIX}_dlq_rejected_total",
            "Total DLQ items rejected by overflow reject strategy",
            ["domain"],
        )
        self._emergency_purge_total = get_or_create_counter(
            f"{self.PREFIX}_dlq_emergency_purge_total",
            "Total DLQ emergency purge executions",
            [],
        )
        # 545 D6: domain input validation rejection counter. Labelled by call
        # site only (3 fixed values) — reason/preview live in the WARNING log
        # payload to keep the metric series count flat.
        self._domain_input_rejected_total = get_or_create_counter(
            f"{self.PREFIX}_dlq_domain_input_rejected_total",
            "Total domain input validations rejected at chokepoints",
            ["site"],
        )
        # 606 D9: terminal-convergence signal for the operator replay paths.
        # Increments when an entry reaches REQUIRES_REVIEW via replay() /
        # retry_entry() — lets operators alert on poison-pill accumulation.
        self._replay_exhausted_total = get_or_create_counter(
            f"{self.PREFIX}_dlq_replay_exhausted_total",
            "Total DLQ entries that exhausted replay attempts (terminal review)",
            ["domain"],
        )
        # 607 D7: force-redrive observability. Occurrence counter — SRE can
        # dashboard/alert on force-redrive frequency (a systemic-problem
        # signal). The DLQ_FORCE_REDRIVE audit event is the compliance record;
        # this is the ops signal (different consumers).
        self._force_redrive_total = get_or_create_counter(
            f"{self.PREFIX}_dlq_force_redrive_total",
            "Total DLQ force-redrive operator cap-overrides",
            ["domain"],
        )
        # 607 D7: severity-escalated signal — a force-redriven entry
        # (operator-asserted fix) that STILL re-converges to REQUIRES_REVIEW,
        # strictly more severe than ordinary exhaustion.
        self._force_redrive_exhausted_total = get_or_create_counter(
            f"{self.PREFIX}_dlq_force_redrive_exhausted_total",
            "Total force-redriven DLQ entries that re-exhausted (asserted fix failed)",
            ["domain"],
        )
        self._size_ratio = get_or_create_gauge(
            f"{self.PREFIX}_dlq_size_ratio",
            "Current DLQ size / max_size ratio",
            ["domain"],
        )
        # On-recovery auto-replay arming state (0/1, empty label). Answers "is the
        # event-driven DLQ auto-replay loop armed right now?" — set at startup
        # (non-I/O links), each arming-probe invocation, and each on-recovery
        # dispatch evaluation. Mirrors the system-control enabled gauge shape.
        self._auto_replay_armed = get_or_create_gauge(
            f"{self.PREFIX}_dlq_auto_replay_armed",
            "1=DLQ auto-replay armed on recovery, 0=disarmed (a prerequisite is missing)",
            [],
        )
        # On-recovery dispatch outcome counter. Passive consumption assurance for
        # the CB-close replay dispatch: outcome in {dispatched,
        # skipped_pro_absent, skipped_disabled, celery_missing, error}.
        self._replay_dispatch_total = get_or_create_counter(
            f"{self.PREFIX}_dlq_replay_dispatch_total",
            "Total on-recovery replay dispatch evaluations by outcome",
            ["outcome"],
        )

        from baldur.metrics.registry import get_or_create_histogram

        self._store_duration_seconds = get_or_create_histogram(
            f"{self.PREFIX}_dlq_store_duration_seconds",
            "Duration of DLQ store operations",
            ["domain"],
            buckets=(0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1, 5, 10),
        )
        self._replay_duration_seconds = get_or_create_histogram(
            f"{self.PREFIX}_dlq_replay_duration_seconds",
            "Duration of DLQ replay operations",
            ["domain"],
            buckets=(0.1, 0.5, 1, 2, 5, 10, 30, 60, 120),
        )
        self._acquire_duration_seconds = get_or_create_histogram(
            f"{self.PREFIX}_dlq_acquire_duration_seconds",
            "Duration of DLQ acquire-for-replay operations",
            ["domain"],
            buckets=(0.0005, 0.001, 0.005, 0.01, 0.05, 0.1, 0.5),
        )

    def record_item_created(self, domain: str, failure_type: str) -> None:
        """Record that a new DLQ item was created."""
        try:
            is_synthetic = self._get_synthetic_label()
            self._items_total.labels(
                domain=domain,
                failure_type=failure_type,
                is_synthetic=is_synthetic,
            ).inc()
            self._created_total.labels(domain=domain).inc()
            logger.debug(
                "metrics.dlq_item_created",
                healing_domain=domain,
                failure_type=failure_type,
                is_synthetic=is_synthetic,
            )
        except Exception as e:
            logger.warning("metrics.record_dlq_creation_failed", error=e)

    def set_pending_count(self, domain: str, count: int) -> None:
        """Set the pending DLQ item count for a domain."""
        try:
            safe_count = self._clamp_non_negative(count, f"dlq_pending_count[{domain}]")
            self._pending_gauge.labels(domain=domain).set(safe_count)
        except Exception as e:
            logger.warning("metrics.set_dlq_pending_failed", error=e)

    def get_pending_count(self, domain: str) -> float:
        """Read the current pending DLQ count gauge for a domain (0.0 if unset).

        Cross-backend read accessor mirroring :meth:`set_pending_count` — the
        drift-before snapshot reads the in-memory gauge through this.
        """
        # NOTE: prometheus_client exposes no public gauge-read API, so
        # `_value.get()` is the only in-process read of a Gauge child. This is a
        # permanent library limitation owned in this file (G45 exempts the
        # gauge's owner from the private-attribute access ban), not a FIXME. The
        # read-accessor test isolates this path, so a prometheus-client upgrade
        # that breaks `_value` is caught there rather than silently.
        try:
            return self._pending_gauge.labels(domain=domain)._value.get()
        except Exception as e:
            logger.warning("metrics.get_dlq_pending_failed", error=e)
            return 0.0

    def set_status_count(self, status: str, count: int) -> None:
        """Set the DLQ item count for a status."""
        try:
            safe_count = self._clamp_non_negative(count, f"dlq_status_count[{status}]")
            self._by_status_gauge.labels(status=status).set(safe_count)
        except Exception as e:
            logger.warning("metrics.set_dlq_status_failed", error=e)

    def record_overflow(self, domain: str, strategy: str) -> None:
        """Record a DLQ overflow event."""
        try:
            self._overflow_total.labels(domain=domain, strategy=strategy).inc()
        except Exception as e:
            logger.warning("metrics.record_dlq_overflow_failed", error=e)

    def record_evicted(self, count: int, strategy: str, domain: str = "") -> None:
        """Record DLQ items evicted by overflow."""
        try:
            self._evicted_total.labels(domain=domain, strategy=strategy).inc(count)
        except Exception as e:
            logger.warning("metrics.record_dlq_evicted_failed", error=e)

    def record_rejected(self, domain: str) -> None:
        """Record a DLQ item rejected by overflow reject strategy."""
        try:
            self._rejected_total.labels(domain=domain).inc()
        except Exception as e:
            logger.warning("metrics.record_dlq_rejected_failed", error=e)

    def record_emergency_purge(self) -> None:
        """Record a DLQ emergency purge execution."""
        try:
            self._emergency_purge_total.inc()
        except Exception as e:
            logger.warning("metrics.record_dlq_emergency_purge_failed", error=e)

    def record_domain_input_rejected(self, site: str) -> None:
        """Record a domain input validation rejection at a chokepoint."""
        try:
            self._domain_input_rejected_total.labels(site=site).inc()
        except Exception as e:
            logger.warning("metrics.record_dlq_domain_input_rejected_failed", error=e)

    def record_replay_exhausted(self, domain: str) -> None:
        """Record a DLQ entry reaching the terminal review state via replay."""
        try:
            self._replay_exhausted_total.labels(domain=domain).inc()
        except Exception as e:
            logger.warning("metrics.record_dlq_replay_exhausted_failed", error=e)

    def record_force_redrive(self, domain: str) -> None:
        """Record a DLQ force-redrive operator cap-override."""
        try:
            self._force_redrive_total.labels(domain=domain).inc()
        except Exception as e:
            logger.warning("metrics.record_dlq_force_redrive_failed", error=e)

    def record_force_redrive_exhausted(self, domain: str) -> None:
        """Record a force-redriven entry re-converging to terminal review."""
        try:
            self._force_redrive_exhausted_total.labels(domain=domain).inc()
        except Exception as e:
            logger.warning("metrics.record_dlq_force_redrive_exhausted_failed", error=e)

    def set_auto_replay_armed(self, armed: bool) -> None:
        """Set the on-recovery auto-replay armed gauge (1=armed, 0=disarmed)."""
        try:
            self._auto_replay_armed.set(1 if armed else 0)
        except Exception as e:
            logger.warning("metrics.set_dlq_auto_replay_armed_failed", error=e)

    def record_replay_dispatch(self, outcome: str) -> None:
        """Record an on-recovery replay dispatch evaluation by outcome."""
        try:
            self._replay_dispatch_total.labels(outcome=outcome).inc()
        except Exception as e:
            logger.warning("metrics.record_dlq_replay_dispatch_failed", error=e)

    def set_size_ratio(self, domain: str, ratio: float) -> None:
        """Set the DLQ size/max_size ratio for a domain."""
        try:
            safe_ratio = (
                self._clamp_percentage(ratio * 100, f"dlq_size_ratio[{domain}]") / 100
            )
            self._size_ratio.labels(domain=domain).set(safe_ratio)
        except Exception as e:
            logger.warning("metrics.set_dlq_size_ratio_failed", error=e)

    def record_store_duration(self, domain: str, duration: float) -> None:
        """Record store_failure() operation duration."""
        try:
            domain = self._resolve_domain(domain)
            self._store_duration_seconds.labels(domain=domain).observe(duration)
        except Exception as e:
            logger.warning("metrics.record_dlq_store_duration_failed", error=e)

    def record_replay_duration(self, domain: str, duration: float) -> None:
        """Record _execute_replay() operation duration."""
        try:
            domain = self._resolve_domain(domain)
            self._replay_duration_seconds.labels(domain=domain).observe(duration)
        except Exception as e:
            logger.warning("metrics.record_dlq_replay_duration_failed", error=e)

    def record_acquire_duration(self, domain: str, duration: float) -> None:
        """Record try_acquire_for_replay() operation duration."""
        try:
            domain = self._resolve_domain(domain)
            self._acquire_duration_seconds.labels(domain=domain).observe(duration)
        except Exception as e:
            logger.warning("metrics.record_dlq_acquire_duration_failed", error=e)
