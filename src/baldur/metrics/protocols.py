"""
MetricsBackend Protocol — Prometheus/OTEL common interface (D7).

Defines the structural contract for metrics backends. Both
BaldurMetrics (Prometheus) and OTELBaldurMetrics must
expose the same recorder attributes.

All implementations must be thread-safe for concurrent metric recording.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from baldur.metrics.recorders.circuit_breaker import CBMetricRecorder
from baldur.metrics.recorders.dlq import DLQMetricRecorder
from baldur.metrics.recorders.infrastructure import InfraMetricRecorder
from baldur.metrics.recorders.replay import ReplayMetricRecorder
from baldur.metrics.recorders.retry import RetryMetricRecorder
from baldur.metrics.recorders.throttle import ThrottleMetricRecorder

__all__ = ["MetricsBackend"]


@runtime_checkable
class MetricsBackend(Protocol):
    """Prometheus/OTEL common interface.

    All implementations must be thread-safe for concurrent metric recording.
    get_metrics() returns this type, ensuring structural compatibility
    between Prometheus and OTEL backends.
    """

    dlq: DLQMetricRecorder
    retry: RetryMetricRecorder
    circuit_breaker: CBMetricRecorder
    replay: ReplayMetricRecorder
    infra: InfraMetricRecorder
    throttle: ThrottleMetricRecorder

    # Convenience methods — both BaldurMetrics (prometheus.py) and
    # OTELBaldurMetrics (otel_backend.py) expose these as flat delegates
    # over the recorder attributes. Declared here so the module-level
    # convenience functions in metrics/prometheus.py type-check.

    def record_dlq_item_created(self, domain: str, failure_type: str) -> None: ...

    def record_retry_attempt(
        self, domain: str, attempt_count: int, outcome: str
    ) -> None: ...

    def record_recovery_time(
        self,
        domain: str,
        resolution_type: str,
        created_at: datetime,
        resolved_at: datetime,
    ) -> None: ...

    def record_sla_breach(self, domain: str) -> None: ...

    def record_circuit_breaker_state_change(
        self,
        service_name: str,
        from_state: str,
        to_state: str,
        cell_id: str = "",
    ) -> None: ...

    def record_circuit_breaker_open_duration(
        self, service_name: str, duration_seconds: float
    ) -> None: ...

    def record_replay_attempt(
        self, domain: str, replay_type: str, success: bool
    ) -> None: ...

    def record_http_request(
        self,
        method: str,
        endpoint: str,
        status_code: int,
        duration_seconds: float,
    ) -> None: ...

    def record_http_error(
        self, method: str, endpoint: str, error_type: str
    ) -> None: ...

    def set_request_queue_depth(self, service: str, depth: int) -> None: ...

    def set_worker_utilization(self, pool_name: str, ratio: float) -> None: ...

    def set_active_connections(self, connection_type: str, count: int) -> None: ...

    def set_latency_percentile(
        self, endpoint: str, percentile: str, value_seconds: float
    ) -> None: ...

    def set_error_rate(self, service: str, rate_percent: float) -> None: ...
