"""
Monitoring Operations Mixin.

Provides methods for monitoring, metrics, and health checks.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from baldur.adapters.memory.layered_repository.base import _default_metrics

if TYPE_CHECKING:
    import threading
    from datetime import datetime

    from baldur.adapters.memory.circuit_breaker import (
        InMemoryCircuitBreakerStateRepository,
    )
    from baldur.adapters.memory.drift_reconciliation import DriftReconciler
    from baldur.adapters.memory.shadow_logger import ShadowLogger
    from baldur.interfaces.repositories import CircuitBreakerStateRepository

logger = structlog.get_logger()


class MonitoringMixin:
    """Mixin providing monitoring and management operations."""

    if TYPE_CHECKING:
        # Host contract — attributes/methods provided via MRO by
        # LayeredRepositoryBase and sibling mixins. Health-state
        # attributes are redeclared with the canonical type so mypy
        # does not infer narrower types from the `reset_l2_health`
        # assignments below and clash with ErrorHandlingMixin's
        # declarations.
        _l1: InMemoryCircuitBreakerStateRepository
        _l2: CircuitBreakerStateRepository | None
        _adapter_type: str
        _sync_interval: float
        _last_sync_time: datetime | None
        _l2_healthy: bool
        _l2_was_unhealthy: bool
        _l2_consecutive_failures: int
        _l2_last_error_time: datetime | None
        _shadow_logger: ShadowLogger
        _drift_reconciler: DriftReconciler
        _metrics: dict[str, Any]
        _metrics_lock: threading.Lock
        _lock: threading.Lock

        def _get_timeout_seconds(self) -> float: ...

    def get_storage_info(self) -> dict[str, Any]:
        """Look up storage info (including L2 state and metrics)."""
        # Snapshot the quarantine quad under self._lock (D6) and the 6
        # consumed counters under the metrics lock — two separate,
        # never-nested critical sections (D5) — then release both before
        # deriving values and assembling the result. The foreign get_stats()
        # / _get_timeout_seconds() calls and the .isoformat() conversion stay
        # outside both sections so neither lock is held across another
        # object's lock.
        with self._lock:
            l2_healthy = self._l2_healthy
            l2_was_unhealthy = self._l2_was_unhealthy
            l2_consecutive_failures = self._l2_consecutive_failures
            l2_last_error_time = self._l2_last_error_time

        with self._metrics_lock:
            timeout_count = self._metrics["l2_timeout_count"]
            sync_failure_count = self._metrics["l2_sync_failure_count"]
            sync_success_count = self._metrics["l2_sync_success_count"]
            latency_total_ms = self._metrics["l2_latency_total_ms"]
            latency_count = self._metrics["l2_latency_count"]
            drift_reconciliation_count = self._metrics["drift_reconciliation_count"]

        avg_latency_ms = 0.0
        if latency_count > 0:
            avg_latency_ms = latency_total_ms / latency_count

        return {
            "l1_type": "memory",
            "l1_count": len(self._l1.get_all_states()),
            "l2_enabled": self._l2 is not None,
            "l2_type": type(self._l2).__name__ if self._l2 else None,
            "l2_adapter_type": self._adapter_type,
            "l2_healthy": l2_healthy,
            "l2_was_unhealthy": l2_was_unhealthy,
            "l2_consecutive_failures": l2_consecutive_failures,
            "l2_last_error_time": (
                l2_last_error_time.isoformat() if l2_last_error_time else None
            ),
            "sync_interval_seconds": self._sync_interval,
            "last_sync_time": (
                self._last_sync_time.isoformat() if self._last_sync_time else None
            ),
            "timeout_ms": self._get_timeout_seconds() * 1000,
            "metrics": {
                "timeout_count": timeout_count,
                "sync_failure_count": sync_failure_count,
                "sync_success_count": sync_success_count,
                "drift_reconciliation_count": drift_reconciliation_count,
                "avg_latency_ms": round(avg_latency_ms, 2),
            },
            "shadow_log": self._shadow_logger.get_stats(),
            "drift_reconciler": self._drift_reconciler.get_stats(),
        }

    def get_l2_health(self) -> dict[str, Any]:
        """Look up the L2 health status."""
        # Snapshot the quarantine quad under self._lock (D6) so the four
        # fields come from one consistent transition; the foreign
        # _get_timeout_seconds() and .isoformat() stay outside the section.
        with self._lock:
            healthy = self._l2_healthy
            was_unhealthy = self._l2_was_unhealthy
            consecutive_failures = self._l2_consecutive_failures
            last_error_time = self._l2_last_error_time

        return {
            "healthy": healthy,
            "was_unhealthy": was_unhealthy,
            "consecutive_failures": consecutive_failures,
            "last_error_time": (
                last_error_time.isoformat() if last_error_time else None
            ),
            "adapter_type": self._adapter_type,
            "timeout_ms": self._get_timeout_seconds() * 1000,
        }

    def reset_l2_health(self) -> None:
        """Reset the L2 health status (on manual recovery)."""
        # Same lost-update class as the failure-count RMW (D6): a writer
        # interleaving this partial reset could resurrect a stale field, so
        # the four-field reset runs as one atomic block under self._lock.
        with self._lock:
            self._l2_healthy = True
            self._l2_was_unhealthy = False
            self._l2_consecutive_failures = 0
            self._l2_last_error_time = None
        logger.info("layered_repo.health_status_reset_manually")

    def get_metrics(self) -> dict[str, Any]:
        """Look up internal metrics."""
        with self._metrics_lock:
            return dict(self._metrics)

    def reset_metrics(self) -> None:
        """Reset metrics (for tests)."""
        # Mutate in place under the lock so the dict object identity stays
        # stable for the unlocked single-key log/audit readers (which read
        # self._metrics.get(...) without the lock and must always see a live
        # dict). The key set comes from the shared _default_metrics() factory,
        # so __init__ and reset cannot drift.
        with self._metrics_lock:
            self._metrics.update(_default_metrics())
