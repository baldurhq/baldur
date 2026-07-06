"""
Prometheus-compatible Metrics for Audit Logging.

Tracks:
- audit_write_total: Total write attempts (by backend, status)
- audit_failure_total: Total failures (by backend, error_type)
- audit_circuit_state: Current circuit breaker state (by backend)
- audit_degraded_mode: Whether system is in degraded mode
"""

from __future__ import annotations

import threading
from datetime import datetime
from typing import Any

import structlog

from baldur.utils.time import utc_now

logger = structlog.get_logger()


class AuditMetrics:
    """
    Prometheus-compatible metrics for audit logging.

    Tracks:
    - audit_write_total: Total write attempts (by backend, status)
    - audit_failure_total: Total failures (by backend, error_type)
    - audit_circuit_state: Current circuit breaker state (by backend)
    - audit_degraded_mode: Whether system is in degraded mode

    Usage:
        metrics = AuditMetrics.get_instance()
        metrics.record_write("LocalFile", success=True)
        metrics.record_failure("CloudWatch", "timeout")
    """

    _instance: AuditMetrics | None = None
    _lock = threading.Lock()

    def __init__(self):
        self._metrics_lock = threading.RLock()

        # Counters
        self._write_total: dict[str, dict[str, int]] = {}  # {backend: {status: count}}
        self._failure_total: dict[
            str, dict[str, int]
        ] = {}  # {backend: {error_type: count}}

        # Gauges
        self._circuit_states: dict[str, str] = {}  # {backend: state}
        self._degraded_mode: bool = False
        self._degraded_since: datetime | None = None

        # Histogram-like data (simplified)
        self._write_durations: dict[str, list[float]] = {}  # {backend: [durations]}

        # WAL metrics (zero-loss guarantee)
        self._wal_writes_total: int = 0
        self._wal_write_failures_total: int = 0
        self._central_writes_total: int = 0
        self._sync_lag_entries: int = 0

    @classmethod
    def get_instance(cls) -> AuditMetrics:
        """Get singleton instance."""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    # =========================================================================
    # WAL metrics (zero-loss guarantee)
    # =========================================================================

    def record_wal_write(self, success: bool = True) -> None:
        """Record a WAL write metric."""
        with self._metrics_lock:
            if success:
                self._wal_writes_total += 1
            else:
                self._wal_write_failures_total += 1

    def record_central_write(self, count: int = 1) -> None:
        """Record a central storage write metric."""
        with self._metrics_lock:
            self._central_writes_total += count

    def set_sync_lag(self, entries: int) -> None:
        """Set the number of sync-lag entries."""
        with self._metrics_lock:
            self._sync_lag_entries = entries

    def get_wal_metrics(self) -> dict[str, Any]:
        """Return WAL-related metrics."""
        with self._metrics_lock:
            return {
                "audit_wal_writes_total": self._wal_writes_total,
                "audit_wal_write_failures_total": self._wal_write_failures_total,
                "audit_central_writes_total": self._central_writes_total,
                "audit_sync_lag_entries": self._sync_lag_entries,
            }

    def record_write(self, backend: str, success: bool, duration_ms: float = 0) -> None:
        """Record a write attempt."""
        with self._metrics_lock:
            status = "success" if success else "failure"

            if backend not in self._write_total:
                self._write_total[backend] = {"success": 0, "failure": 0}
            self._write_total[backend][status] += 1

            if duration_ms > 0:
                if backend not in self._write_durations:
                    self._write_durations[backend] = []
                # Keep last 100 durations
                self._write_durations[backend].append(duration_ms)
                if len(self._write_durations[backend]) > 100:
                    self._write_durations[backend] = self._write_durations[backend][
                        -100:
                    ]

    def record_failure(self, backend: str, error_type: str) -> None:
        """Record a failure with error type."""
        with self._metrics_lock:
            if backend not in self._failure_total:
                self._failure_total[backend] = {}
            if error_type not in self._failure_total[backend]:
                self._failure_total[backend][error_type] = 0
            self._failure_total[backend][error_type] += 1

    def set_circuit_state(self, backend: str, state: str) -> None:
        """Update circuit breaker state."""
        with self._metrics_lock:
            self._circuit_states[backend] = state

    def set_degraded_mode(self, degraded: bool) -> None:
        """Set degraded mode status."""
        with self._metrics_lock:
            was_degraded = self._degraded_mode
            self._degraded_mode = degraded

            if degraded and not was_degraded:
                self._degraded_since = utc_now()
                logger.warning("audit_metrics.entered_degraded_mode")
            elif not degraded and was_degraded:
                self._degraded_since = None
                logger.info("audit_metrics.exited_degraded_mode")

    def is_degraded(self) -> bool:
        """Check if in degraded mode."""
        with self._metrics_lock:
            return self._degraded_mode

    def get_metrics(self) -> dict[str, Any]:
        """
        Get all metrics in Prometheus-compatible format.

        Returns dict that can be exposed via /metrics endpoint.
        """
        with self._metrics_lock:
            metrics = {
                "audit_write_total": self._write_total.copy(),
                "audit_failure_total": self._failure_total.copy(),
                "audit_circuit_state": self._circuit_states.copy(),
                "audit_degraded_mode": 1 if self._degraded_mode else 0,
                "audit_degraded_since": (
                    self._degraded_since.isoformat() if self._degraded_since else None
                ),
                # WAL metrics (zero-loss guarantee)
                "audit_wal_writes_total": self._wal_writes_total,
                "audit_wal_write_failures_total": self._wal_write_failures_total,
                "audit_central_writes_total": self._central_writes_total,
                "audit_sync_lag_entries": self._sync_lag_entries,
            }

            # Add duration stats
            duration_stats = {}
            for backend, durations in self._write_durations.items():
                if durations:
                    duration_stats[backend] = {
                        "avg_ms": sum(durations) / len(durations),
                        "max_ms": max(durations),
                        "min_ms": min(durations),
                        "count": len(durations),
                    }
            metrics["audit_write_duration"] = duration_stats

            return metrics

    def get_prometheus_format(self) -> str:
        """
        Get metrics in Prometheus text exposition format.

        Can be directly served at /metrics endpoint.
        """
        lines = []
        metrics = self.get_metrics()

        # Write totals
        lines.append("# HELP audit_write_total Total audit write attempts")
        lines.append("# TYPE audit_write_total counter")
        for backend, statuses in metrics["audit_write_total"].items():
            for status, count in statuses.items():
                lines.append(
                    f'audit_write_total{{backend="{backend}",status="{status}"}} {count}'
                )

        # Failure totals
        lines.append("# HELP audit_failure_total Total audit failures by type")
        lines.append("# TYPE audit_failure_total counter")
        for backend, errors in metrics["audit_failure_total"].items():
            for error_type, count in errors.items():
                lines.append(
                    f'audit_failure_total{{backend="{backend}",error_type="{error_type}"}} {count}'
                )

        # Circuit states
        lines.append(
            "# HELP audit_circuit_state Circuit breaker state (0=closed, 1=open, 2=half_open)"
        )
        lines.append("# TYPE audit_circuit_state gauge")
        state_values = {"closed": 0, "open": 1, "half_open": 2}
        for backend, state in metrics["audit_circuit_state"].items():
            value = state_values.get(state, -1)
            lines.append(f'audit_circuit_state{{backend="{backend}"}} {value}')

        # Degraded mode
        lines.append("# HELP audit_degraded_mode Whether audit is in degraded mode")
        lines.append("# TYPE audit_degraded_mode gauge")
        lines.append(f"audit_degraded_mode {metrics['audit_degraded_mode']}")

        # WAL metrics (zero-loss guarantee)
        lines.append("# HELP audit_wal_writes_total Total WAL writes")
        lines.append("# TYPE audit_wal_writes_total counter")
        lines.append(f"audit_wal_writes_total {metrics['audit_wal_writes_total']}")

        lines.append(
            "# HELP audit_wal_write_failures_total Total WAL write failures (CRITICAL)"
        )
        lines.append("# TYPE audit_wal_write_failures_total counter")
        lines.append(
            f"audit_wal_write_failures_total {metrics['audit_wal_write_failures_total']}"
        )

        lines.append("# HELP audit_central_writes_total Total central storage writes")
        lines.append("# TYPE audit_central_writes_total counter")
        lines.append(
            f"audit_central_writes_total {metrics['audit_central_writes_total']}"
        )

        lines.append("# HELP audit_sync_lag_entries Current WAL to central sync lag")
        lines.append("# TYPE audit_sync_lag_entries gauge")
        lines.append(f"audit_sync_lag_entries {metrics['audit_sync_lag_entries']}")

        return "\n".join(lines)

    def reset(self) -> None:
        """Reset all metrics (for testing)."""
        with self._metrics_lock:
            self._write_total.clear()
            self._failure_total.clear()
            self._circuit_states.clear()
            self._write_durations.clear()
            self._degraded_mode = False
            self._degraded_since = None
            # Reset WAL metrics
            self._wal_writes_total = 0
            self._wal_write_failures_total = 0
            self._central_writes_total = 0
            self._sync_lag_entries = 0


def get_audit_metrics() -> AuditMetrics:
    """Get the audit metrics instance."""
    return AuditMetrics.get_instance()


__all__ = ["AuditMetrics", "get_audit_metrics"]
