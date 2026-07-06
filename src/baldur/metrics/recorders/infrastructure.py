"""
Infrastructure metric recorder — HTTP/RED, Golden Signals, Security, Mesh, DI, Capacity.

Owns all infrastructure-related Prometheus metrics that don't belong to
DLQ, Retry, Circuit Breaker, or Replay domains.
"""

from __future__ import annotations

import time
from contextlib import contextmanager

import structlog

from baldur.metrics.recorders.base import BaseMetricRecorder
from baldur.metrics.registry import (
    get_or_create_counter,
    get_or_create_gauge,
    get_or_create_histogram,
)

logger = structlog.get_logger()

__all__ = ["InfraMetricRecorder"]

# Try to import Info from prometheus_client
try:
    from prometheus_client import Info

    INFO_AVAILABLE = True
except ImportError:
    INFO_AVAILABLE = False


class InfraMetricRecorder(BaseMetricRecorder):
    """Infrastructure metric definitions and recording.

    Covers: HTTP/RED metrics, Four Golden Signals (saturation),
    Security, Circuit Mesh Coordinator, DI Fallback, Capacity Reservation,
    GIL contention, and system info.
    """

    def __init__(self) -> None:
        # =================================================================
        # RED Metrics (Rate, Errors, Duration)
        # =================================================================
        self._http_requests_total = get_or_create_counter(
            f"{self.PREFIX}_http_requests_total",
            "Total HTTP requests (Rate)",
            ["method", "endpoint", "status_code"],
        )
        self._http_request_duration = get_or_create_histogram(
            f"{self.PREFIX}_http_request_duration_seconds",
            "HTTP request duration in seconds (Duration)",
            ["method", "endpoint"],
            buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
        )
        self._http_errors_total = get_or_create_counter(
            f"{self.PREFIX}_http_request_errors_total",
            "Total HTTP request errors (Errors)",
            ["method", "endpoint", "error_type"],
        )

        # =================================================================
        # Four Golden Signals — Saturation
        # =================================================================
        self._queue_depth = get_or_create_gauge(
            f"{self.PREFIX}_request_queue_depth",
            "Current request queue depth (Saturation)",
            ["service"],
        )
        self._worker_utilization = get_or_create_gauge(
            f"{self.PREFIX}_worker_utilization_ratio",
            "Worker pool utilization ratio 0.0-1.0 (Saturation)",
            ["pool_name"],
        )
        self._active_connections = get_or_create_gauge(
            f"{self.PREFIX}_active_connections",
            "Number of active connections (Saturation)",
            ["connection_type"],
        )
        self._latency_percentiles = get_or_create_gauge(
            f"{self.PREFIX}_request_latency_percentile_seconds",
            "Request latency percentiles (Latency)",
            ["percentile", "endpoint"],
        )
        self._error_rate_percent = get_or_create_gauge(
            f"{self.PREFIX}_error_rate_percent",
            "Current error rate percentage (Errors)",
            ["service"],
        )

        # GIL Contention (Meta-Watchdog probe output)
        self._gil_contention_p90_ms = get_or_create_gauge(
            f"{self.PREFIX}_gil_contention_p90_ms",
            "GIL contention P90 latency in milliseconds",
            [],
        )

        # Info metric
        self._info = None
        if INFO_AVAILABLE:
            try:
                self._info = Info(
                    f"{self.PREFIX}_info",
                    "Baldur system information",
                )
            except ValueError:
                pass

        # =================================================================
        # Security Metrics
        # =================================================================
        self._security_incidents = get_or_create_counter(
            f"{self.PREFIX}_security_incidents_total",
            "Total security incidents",
            ["incident_type", "severity"],
        )

        # =================================================================
        # Circuit Mesh Coordinator Metrics
        # =================================================================
        self._mesh_overrides_active = get_or_create_gauge(
            f"{self.PREFIX}_mesh_overrides_active",
            "Current active mesh threshold overrides",
            [],
        )
        self._mesh_override_applied_total = get_or_create_counter(
            f"{self.PREFIX}_mesh_override_applied_total",
            "Total mesh threshold overrides applied",
            [],
        )
        self._mesh_override_released_total = get_or_create_counter(
            f"{self.PREFIX}_mesh_override_released_total",
            "Total mesh threshold overrides released",
            [],
        )
        self._mesh_override_expired_total = get_or_create_counter(
            f"{self.PREFIX}_mesh_override_expired_total",
            "Total mesh threshold overrides expired by TTL",
            [],
        )
        self._mesh_override_renewed_total = get_or_create_counter(
            f"{self.PREFIX}_mesh_override_renewed_total",
            "Total mesh threshold override TTL renewals",
            [],
        )
        self._mesh_preemptive_fallback_total = get_or_create_counter(
            f"{self.PREFIX}_mesh_preemptive_fallback_total",
            "Total preemptive fallback activations",
            [],
        )
        self._mesh_fast_recovery_total = get_or_create_counter(
            f"{self.PREFIX}_mesh_fast_recovery_total",
            "Total fast-recovery overrides applied",
            [],
        )
        self._mesh_escalation_total = get_or_create_counter(
            f"{self.PREFIX}_mesh_escalation_total",
            "Total escalations to EmergencyCoordinator",
            [],
        )
        self._mesh_circular_dependency_total = get_or_create_counter(
            f"{self.PREFIX}_mesh_circular_dependency_detected_total",
            "Total circular dependency detections in mesh",
            [],
        )
        self._mesh_override_store_drift_total = get_or_create_counter(
            f"{self.PREFIX}_mesh_override_store_drift_total",
            "Total L1-L2 drift detections in mesh override store",
            [],
        )
        self._mesh_override_store_drift_repaired_total = get_or_create_counter(
            f"{self.PREFIX}_mesh_override_store_drift_repaired_total",
            "Total L1-L2 auto-repairs in mesh override store",
            [],
        )
        self._mesh_recovery_duration = get_or_create_histogram(
            f"{self.PREFIX}_mesh_recovery_duration_seconds",
            "Duration from downstream CB OPEN to CLOSED recovery in seconds",
            [],
            buckets=(5, 10, 30, 60, 120, 300, 600, 1800, 3600),
        )

        # =================================================================
        # DI Fallback Metrics
        # =================================================================
        self._di_fallback_total = get_or_create_counter(
            f"{self.PREFIX}_di_fallback_total",
            "DI fallback to in-memory adapter",
            ["service", "adapter"],
        )

        # =================================================================
        # Capacity Reservation Metrics
        # =================================================================
        self._capacity_warmup_total = get_or_create_counter(
            f"{self.PREFIX}_capacity_warmup_total",
            "Total warm-up executions",
            ["event_id", "outcome"],
        )
        self._capacity_warmup_duration = get_or_create_histogram(
            f"{self.PREFIX}_capacity_warmup_duration_seconds",
            "Warm-up execution duration in seconds",
            [],
            buckets=(0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0),
        )
        self._capacity_cooldown_total = get_or_create_counter(
            f"{self.PREFIX}_capacity_cooldown_total",
            "Total cool-down executions",
            ["event_id", "outcome"],
        )
        self._capacity_active_events = get_or_create_gauge(
            f"{self.PREFIX}_capacity_active_events",
            "Currently active scheduled events",
            [],
        )
        self._capacity_rate_multiplier = get_or_create_gauge(
            f"{self.PREFIX}_capacity_rate_multiplier",
            "Currently applied rate multiplier",
            [],
        )
        self._capacity_pool_multiplier = get_or_create_gauge(
            f"{self.PREFIX}_capacity_pool_multiplier",
            "Currently applied pool multiplier",
            [],
        )

    # =====================================================================
    # RED Metrics Recording
    # =====================================================================

    def record_http_request(
        self,
        method: str,
        endpoint: str,
        status_code: int,
        duration_seconds: float,
    ) -> None:
        """Record an HTTP request (RED metrics: Rate + Duration)."""
        try:
            self._http_requests_total.labels(
                method=method,
                endpoint=endpoint,
                status_code=str(status_code),
            ).inc()
            self._http_request_duration.labels(
                method=method, endpoint=endpoint
            ).observe(duration_seconds)
            logger.debug(
                "metrics.http_request",
                method=method,
                endpoint=endpoint,
                status_code=status_code,
                duration_seconds=duration_seconds,
            )
        except Exception as e:
            logger.warning("metrics.record_http_request_failed", error=e)

    def record_http_error(self, method: str, endpoint: str, error_type: str) -> None:
        """Record an HTTP request error (RED metrics: Errors)."""
        try:
            self._http_errors_total.labels(
                method=method, endpoint=endpoint, error_type=error_type
            ).inc()
            logger.debug(
                "metrics.http_error",
                method=method,
                endpoint=endpoint,
                error_type=error_type,
            )
        except Exception as e:
            logger.warning("metrics.record_http_error_failed", error=e)

    @contextmanager
    def http_request_timer(self, method: str, endpoint: str):
        """Context manager for timing HTTP requests."""
        start_time = time.perf_counter()
        error_occurred = False
        error_type = None
        try:
            yield
        except Exception as e:
            error_occurred = True
            error_type = type(e).__name__
            raise
        finally:
            duration = time.perf_counter() - start_time
            self._http_request_duration.labels(
                method=method, endpoint=endpoint
            ).observe(duration)
            if error_occurred and error_type:
                self.record_http_error(method, endpoint, error_type)

    # =====================================================================
    # Golden Signals — Saturation
    # =====================================================================

    def set_request_queue_depth(self, service: str, depth: int) -> None:
        """Set current request queue depth (Saturation signal)."""
        try:
            safe_depth = self._clamp_non_negative(
                depth, f"request_queue_depth[{service}]"
            )
            self._queue_depth.labels(service=service).set(safe_depth)
        except Exception as e:
            logger.warning("metrics.set_queue_depth_failed", error=e)

    def set_worker_utilization(self, pool_name: str, ratio: float) -> None:
        """Set worker pool utilization ratio (Saturation signal)."""
        try:
            safe_ratio = max(0.0, min(1.0, ratio))
            if ratio < 0.0 or ratio > 1.0:
                logger.warning(
                    "metrics.clamped",
                    pool_name=pool_name,
                    ratio=ratio,
                    safe_ratio=safe_ratio,
                )
            self._worker_utilization.labels(pool_name=pool_name).set(safe_ratio)
        except Exception as e:
            logger.warning("metrics.set_worker_utilization_failed", error=e)

    def set_active_connections(self, connection_type: str, count: int) -> None:
        """Set number of active connections (Saturation signal)."""
        try:
            safe_count = self._clamp_non_negative(
                count, f"active_connections[{connection_type}]"
            )
            self._active_connections.labels(connection_type=connection_type).set(
                safe_count
            )
        except Exception as e:
            logger.warning("metrics.set_active_connections_failed", error=e)

    def set_latency_percentile(
        self, endpoint: str, percentile: str, value_seconds: float
    ) -> None:
        """Set request latency percentile (Latency signal)."""
        try:
            safe_value = max(0.0, value_seconds)
            self._latency_percentiles.labels(
                percentile=percentile, endpoint=endpoint
            ).set(safe_value)
        except Exception as e:
            logger.warning("metrics.set_latency_percentile_failed", error=e)

    def set_error_rate(self, service: str, rate_percent: float) -> None:
        """Set current error rate percentage (Errors signal)."""
        try:
            safe_rate = self._clamp_percentage(
                rate_percent, f"error_rate_percent[{service}]"
            )
            self._error_rate_percent.labels(service=service).set(safe_rate)
        except Exception as e:
            logger.warning("metrics.set_error_rate_failed", error=e)

    # =====================================================================
    # Utility
    # =====================================================================

    def set_info(self, info_dict: dict[str, str]) -> None:
        """Set the info metric."""
        if self._info is None:
            return
        try:
            self._info.info(info_dict)
        except Exception as e:
            logger.warning("metrics.set_info_failed", error=e)

    # =====================================================================
    # Security
    # =====================================================================

    def record_security_incident(self, incident_type: str, severity: str) -> None:
        """Record a security incident."""
        try:
            self._security_incidents.labels(
                incident_type=incident_type, severity=severity
            ).inc()
        except Exception as e:
            logger.warning("metrics.record_security_incident_failed", error=e)

    # =====================================================================
    # Mesh Coordinator
    # =====================================================================

    def set_mesh_overrides_active(self, count: int) -> None:
        """Set current active mesh threshold overrides."""
        try:
            self._mesh_overrides_active.set(count)
        except Exception as e:
            logger.warning("metrics.set_mesh_overrides_failed", error=e)

    def record_mesh_override_applied(self) -> None:
        """Record a mesh threshold override applied."""
        try:
            self._mesh_override_applied_total.inc()
        except Exception as e:
            logger.warning("metrics.record_mesh_applied_failed", error=e)

    def record_mesh_override_released(self) -> None:
        """Record a mesh threshold override released."""
        try:
            self._mesh_override_released_total.inc()
        except Exception as e:
            logger.warning("metrics.record_mesh_released_failed", error=e)

    def record_mesh_override_expired(self) -> None:
        """Record a mesh threshold override expired."""
        try:
            self._mesh_override_expired_total.inc()
        except Exception as e:
            logger.warning("metrics.record_mesh_expired_failed", error=e)

    def record_mesh_override_renewed(self) -> None:
        """Record a mesh threshold override TTL renewed."""
        try:
            self._mesh_override_renewed_total.inc()
        except Exception as e:
            logger.warning("metrics.record_mesh_renewed_failed", error=e)

    # =====================================================================
    # DI Fallback
    # =====================================================================

    def record_di_fallback(self, service: str, adapter: str) -> None:
        """Record DI fallback to in-memory adapter."""
        try:
            self._di_fallback_total.labels(service=service, adapter=adapter).inc()
        except Exception as e:
            logger.warning("metrics.record_di_fallback_failed", error=e)

    # =====================================================================
    # Capacity Reservation
    # =====================================================================

    def record_capacity_warmup(self, event_id: str, outcome: str) -> None:
        """Record a capacity warm-up execution."""
        try:
            self._capacity_warmup_total.labels(event_id=event_id, outcome=outcome).inc()
        except Exception as e:
            logger.warning("metrics.record_capacity_warmup_failed", error=e)

    def record_capacity_cooldown(self, event_id: str, outcome: str) -> None:
        """Record a capacity cool-down execution."""
        try:
            self._capacity_cooldown_total.labels(
                event_id=event_id, outcome=outcome
            ).inc()
        except Exception as e:
            logger.warning("metrics.record_capacity_cooldown_failed", error=e)

    def set_capacity_active_events(self, count: int) -> None:
        """Set currently active scheduled events count."""
        try:
            self._capacity_active_events.set(count)
        except Exception as e:
            logger.warning("metrics.set_capacity_events_failed", error=e)

    def set_capacity_rate_multiplier(self, value: float) -> None:
        """Set currently applied rate multiplier."""
        try:
            self._capacity_rate_multiplier.set(value)
        except Exception as e:
            logger.warning("metrics.set_capacity_rate_failed", error=e)

    def set_capacity_pool_multiplier(self, value: float) -> None:
        """Set currently applied pool multiplier."""
        try:
            self._capacity_pool_multiplier.set(value)
        except Exception as e:
            logger.warning("metrics.set_capacity_pool_failed", error=e)
