"""
Prometheus metrics facade for the baldur system.

Thin facade — metric definitions are owned by domain-specific recorders
in metrics/recorders/. This module only composes recorders and provides
the singleton get_metrics() / reset_metrics() pair.

For domain-labeled metrics, always use module-level convenience functions
or services/metrics/recorders entry points. These apply resolve_domain_label()
for cardinality enforcement.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from datetime import datetime
from typing import TYPE_CHECKING, cast

import structlog

logger = structlog.get_logger()

# Check prometheus_client availability
try:
    from prometheus_client import Counter, Gauge, Histogram  # noqa: F401

    PROMETHEUS_AVAILABLE = True
except ImportError:
    PROMETHEUS_AVAILABLE = False

if TYPE_CHECKING:
    from baldur.metrics.protocols import MetricsBackend
    from baldur.metrics.recorders.circuit_breaker import CBMetricRecorder
    from baldur.metrics.recorders.dlq import DLQMetricRecorder
    from baldur.metrics.recorders.infrastructure import InfraMetricRecorder
    from baldur.metrics.recorders.replay import ReplayMetricRecorder
    from baldur.metrics.recorders.retry import RetryMetricRecorder


class BaldurMetrics:
    """Thin facade — composes domain-specific recorders.

    Metric definitions are owned by each recorder in metrics/recorders/.
    This class only creates recorder instances and exposes them as attributes.

    Satisfies the MetricsBackend Protocol (metrics/protocols.py).
    """

    def __init__(self, prefix: str = "baldur") -> None:  # noqa: PLR0915
        self.prefix = prefix
        self._initialized = False

        if not PROMETHEUS_AVAILABLE:
            logger.warning("prometheus.unavailable")
            return

        from baldur.metrics.recorders.auto_tuning import AutoTuningMetricRecorder
        from baldur.metrics.recorders.bulkhead import BulkheadMetricRecorder
        from baldur.metrics.recorders.canary import CanaryMetricRecorder
        from baldur.metrics.recorders.circuit_breaker import CBMetricRecorder
        from baldur.metrics.recorders.correlation_engine import (
            CorrelationEngineMetricRecorder,
        )
        from baldur.metrics.recorders.corruption_shield import (
            CorruptionShieldMetricRecorder,
        )
        from baldur.metrics.recorders.daemon_worker import (
            DaemonWorkerMetricRecorder,
        )
        from baldur.metrics.recorders.daily_report import DailyReportMetricRecorder
        from baldur.metrics.recorders.dlq import DLQMetricRecorder
        from baldur.metrics.recorders.emergency_mode import (
            EmergencyModeMetricRecorder,
        )
        from baldur.metrics.recorders.entitlement import EntitlementMetricRecorder
        from baldur.metrics.recorders.event_bus import EventBusMetricRecorder
        from baldur.metrics.recorders.executor import ExecutorMetricRecorder
        from baldur.metrics.recorders.forecaster import ForecasterMetricRecorder
        from baldur.metrics.recorders.governance import GovernanceMetricRecorder
        from baldur.metrics.recorders.health_check import (
            HealthCheckMetricRecorder,
        )
        from baldur.metrics.recorders.hedging import HedgingMetricRecorder
        from baldur.metrics.recorders.idempotency import IdempotencyMetricRecorder
        from baldur.metrics.recorders.infrastructure import InfraMetricRecorder
        from baldur.metrics.recorders.learning import LearningMetricRecorder
        from baldur.metrics.recorders.notification import (
            NotificationMetricRecorder,
        )
        from baldur.metrics.recorders.pool_monitor import PoolMetricRecorder
        from baldur.metrics.recorders.postmortem import PostmortemMetricRecorder
        from baldur.metrics.recorders.protect import ProtectMetricRecorder
        from baldur.metrics.recorders.recommendation import (
            RecommendationMetricRecorder,
        )
        from baldur.metrics.recorders.replay import ReplayMetricRecorder
        from baldur.metrics.recorders.retry import RetryMetricRecorder
        from baldur.metrics.recorders.runtime_config import (
            RuntimeConfigMetricRecorder,
        )
        from baldur.metrics.recorders.shutdown import ShutdownMetricRecorder
        from baldur.metrics.recorders.system_control import (
            SystemControlMetricRecorder,
        )
        from baldur.metrics.recorders.throttle import ThrottleMetricRecorder
        from baldur.metrics.recorders.watchdog import WatchdogMetricRecorder

        self.dlq: DLQMetricRecorder = DLQMetricRecorder()
        self.retry: RetryMetricRecorder = RetryMetricRecorder()
        self.circuit_breaker: CBMetricRecorder = CBMetricRecorder()
        self.replay: ReplayMetricRecorder = ReplayMetricRecorder()
        self.infra: InfraMetricRecorder = InfraMetricRecorder()
        self.throttle: ThrottleMetricRecorder = ThrottleMetricRecorder()
        self.correlation_engine: CorrelationEngineMetricRecorder = (
            CorrelationEngineMetricRecorder()
        )
        self.auto_tuning: AutoTuningMetricRecorder = AutoTuningMetricRecorder()
        self.recommendation: RecommendationMetricRecorder = (
            RecommendationMetricRecorder()
        )
        self.health_check: HealthCheckMetricRecorder = HealthCheckMetricRecorder()
        self.shutdown: ShutdownMetricRecorder = ShutdownMetricRecorder()
        self.system_control: SystemControlMetricRecorder = SystemControlMetricRecorder()
        self.emergency_mode: EmergencyModeMetricRecorder = EmergencyModeMetricRecorder()
        self.event_bus: EventBusMetricRecorder = EventBusMetricRecorder()
        self.hedging: HedgingMetricRecorder = HedgingMetricRecorder()
        self.pool_monitor: PoolMetricRecorder = PoolMetricRecorder()
        self.canary: CanaryMetricRecorder = CanaryMetricRecorder()
        self.runtime_config: RuntimeConfigMetricRecorder = RuntimeConfigMetricRecorder()
        self.corruption_shield: CorruptionShieldMetricRecorder = (
            CorruptionShieldMetricRecorder()
        )
        self.learning: LearningMetricRecorder = LearningMetricRecorder()
        self.forecaster: ForecasterMetricRecorder = ForecasterMetricRecorder()
        self.daily_report: DailyReportMetricRecorder = DailyReportMetricRecorder()
        self.watchdog: WatchdogMetricRecorder = WatchdogMetricRecorder()
        self.notification: NotificationMetricRecorder = NotificationMetricRecorder()
        self.postmortem: PostmortemMetricRecorder = PostmortemMetricRecorder()
        self.governance: GovernanceMetricRecorder = GovernanceMetricRecorder()
        self.entitlement: EntitlementMetricRecorder = EntitlementMetricRecorder()
        self.protect: ProtectMetricRecorder = ProtectMetricRecorder()
        self.idempotency: IdempotencyMetricRecorder = IdempotencyMetricRecorder()
        self.executor: ExecutorMetricRecorder = ExecutorMetricRecorder()
        self.daemon_workers: DaemonWorkerMetricRecorder = DaemonWorkerMetricRecorder()
        self.bulkhead: BulkheadMetricRecorder = BulkheadMetricRecorder()
        self._initialized = True

    # =========================================================================
    # Backward-compatible delegate methods (for gradual caller migration)
    #
    # These delegate to the appropriate recorder. Callers should migrate to
    # metrics.dlq.method(), metrics.retry.method(), etc. directly.
    # =========================================================================

    # --- DLQ ---
    def record_dlq_item_created(self, domain: str, failure_type: str) -> None:
        if not self._initialized:
            return
        self.dlq.record_item_created(domain, failure_type)

    def set_dlq_status_count(self, status: str, count: int) -> None:
        if not self._initialized:
            return
        self.dlq.set_status_count(status, count)

    def record_dlq_overflow(self, domain: str, strategy: str) -> None:
        if not self._initialized:
            return
        self.dlq.record_overflow(domain, strategy)

    def record_dlq_evicted(self, count: int, strategy: str, domain: str = "") -> None:
        if not self._initialized:
            return
        self.dlq.record_evicted(count, strategy, domain)

    def record_dlq_rejected(self, domain: str) -> None:
        if not self._initialized:
            return
        self.dlq.record_rejected(domain)

    def record_dlq_emergency_purge(self) -> None:
        if not self._initialized:
            return
        self.dlq.record_emergency_purge()

    def record_dlq_domain_input_rejected(self, site: str) -> None:
        if not self._initialized:
            return
        self.dlq.record_domain_input_rejected(site)

    def set_dlq_size_ratio(self, domain: str, ratio: float) -> None:
        if not self._initialized:
            return
        self.dlq.set_size_ratio(domain, ratio)

    # --- Retry / Recovery ---
    def record_retry_attempt(
        self, domain: str, attempt_count: int, outcome: str
    ) -> None:
        if not self._initialized:
            return
        self.retry.record_attempt(domain, attempt_count, outcome)

    def record_retry(
        self, domain: str, success: bool, delay: float | None = None
    ) -> None:
        if not self._initialized:
            return
        self.retry.record_retry(domain, success, delay)

    def set_retry_success_rate(self, domain: str, rate: float) -> None:
        if not self._initialized:
            return
        self.retry.set_success_rate(domain, rate)

    def record_recovery_time(
        self,
        domain: str,
        resolution_type: str,
        created_at: datetime,
        resolved_at: datetime,
    ) -> None:
        if not self._initialized:
            return
        self.retry.record_recovery_time(
            domain, resolution_type, created_at, resolved_at
        )

    def record_sla_breach(self, domain: str) -> None:
        if not self._initialized:
            return
        self.retry.record_sla_breach(domain)

    # --- Circuit Breaker ---
    def set_circuit_state(
        self, service_name: str, state: str, cell_id: str = ""
    ) -> None:
        if not self._initialized:
            return
        self.circuit_breaker.set_state(service_name, state, cell_id)

    def record_circuit_failure(self, service_name: str) -> None:
        if not self._initialized:
            return
        self.circuit_breaker.record_failure(service_name)

    def record_circuit_trip(self, service_name: str) -> None:
        if not self._initialized:
            return
        self.circuit_breaker.record_trip(service_name)

    def record_circuit_breaker_state_change(
        self,
        service_name: str,
        from_state: str,
        to_state: str,
        cell_id: str = "",
    ) -> None:
        if not self._initialized:
            return
        self.circuit_breaker.record_state_change(
            service_name, from_state, to_state, cell_id
        )

    def record_circuit_breaker_open_duration(
        self, service_name: str, duration_seconds: float
    ) -> None:
        if not self._initialized:
            return
        self.circuit_breaker.record_open_duration(service_name, duration_seconds)

    # --- Replay ---
    def record_replay_attempt(
        self, domain: str, replay_type: str, success: bool
    ) -> None:
        if not self._initialized:
            return
        self.replay.record_attempt(domain, replay_type, success)

    def record_replay(
        self, domain: str, result: str, duration: float | None = None
    ) -> None:
        if not self._initialized:
            return
        self.replay.record_replay(domain, result, duration)

    # --- Security ---
    def record_security_incident(self, incident_type: str, severity: str) -> None:
        if not self._initialized:
            return
        self.infra.record_security_incident(incident_type, severity)

    # --- HTTP / RED Metrics ---
    def record_http_request(
        self,
        method: str,
        endpoint: str,
        status_code: int,
        duration_seconds: float,
    ) -> None:
        if not self._initialized:
            return
        self.infra.record_http_request(method, endpoint, status_code, duration_seconds)

    def record_http_error(self, method: str, endpoint: str, error_type: str) -> None:
        if not self._initialized:
            return
        self.infra.record_http_error(method, endpoint, error_type)

    @contextmanager
    def http_request_timer(self, method: str, endpoint: str):
        if not self._initialized:
            yield
            return
        with self.infra.http_request_timer(method, endpoint):
            yield

    # --- Saturation / Golden Signals ---
    def set_request_queue_depth(self, service: str, depth: int) -> None:
        if not self._initialized:
            return
        self.infra.set_request_queue_depth(service, depth)

    def set_worker_utilization(self, pool_name: str, ratio: float) -> None:
        if not self._initialized:
            return
        self.infra.set_worker_utilization(pool_name, ratio)

    def set_active_connections(self, connection_type: str, count: int) -> None:
        if not self._initialized:
            return
        self.infra.set_active_connections(connection_type, count)

    def set_latency_percentile(
        self, endpoint: str, percentile: str, value_seconds: float
    ) -> None:
        if not self._initialized:
            return
        self.infra.set_latency_percentile(endpoint, percentile, value_seconds)

    def set_error_rate(self, service: str, rate_percent: float) -> None:
        if not self._initialized:
            return
        self.infra.set_error_rate(service, rate_percent)

    def set_info(self, info_dict: dict[str, str]) -> None:
        if not self._initialized:
            return
        self.infra.set_info(info_dict)

    @contextmanager
    def timer(self, domain: str, metric_type: str = "replay"):
        if not self._initialized:
            yield
            return
        start_time = time.perf_counter()
        try:
            yield
        finally:
            duration = time.perf_counter() - start_time
            if metric_type == "replay":
                self.replay.record_replay(domain, "timed", duration)

    # --- Mesh Coordinator ---
    def set_mesh_overrides_active(self, count: int) -> None:
        if not self._initialized:
            return
        self.infra.set_mesh_overrides_active(count)

    def record_mesh_override_applied(self) -> None:
        if not self._initialized:
            return
        self.infra.record_mesh_override_applied()

    def record_mesh_override_released(self) -> None:
        if not self._initialized:
            return
        self.infra.record_mesh_override_released()

    def record_mesh_override_expired(self) -> None:
        if not self._initialized:
            return
        self.infra.record_mesh_override_expired()

    def record_mesh_override_renewed(self) -> None:
        if not self._initialized:
            return
        self.infra.record_mesh_override_renewed()

    # --- DI Fallback ---
    def record_di_fallback(self, service: str, adapter: str) -> None:
        if not self._initialized:
            return
        self.infra.record_di_fallback(service, adapter)

    # --- Capacity ---
    def record_capacity_warmup(self, event_id: str, outcome: str) -> None:
        if not self._initialized:
            return
        self.infra.record_capacity_warmup(event_id, outcome)

    def record_capacity_cooldown(self, event_id: str, outcome: str) -> None:
        if not self._initialized:
            return
        self.infra.record_capacity_cooldown(event_id, outcome)

    def set_capacity_active_events(self, count: int) -> None:
        if not self._initialized:
            return
        self.infra.set_capacity_active_events(count)

    def set_capacity_rate_multiplier(self, value: float) -> None:
        if not self._initialized:
            return
        self.infra.set_capacity_rate_multiplier(value)

    def set_capacity_pool_multiplier(self, value: float) -> None:
        if not self._initialized:
            return
        self.infra.set_capacity_pool_multiplier(value)


def _create_metrics() -> MetricsBackend:
    prefix = "baldur"
    try:
        from baldur.settings.observability import get_observability_settings

        if get_observability_settings().effective_backend == "otel":
            from baldur.metrics.otel_backend import OTELBaldurMetrics

            # OTELBaldurMetrics uses _OTEL* recorder subclasses; the convenience
            # methods declared on MetricsBackend are satisfied structurally, but
            # mypy compares concrete recorder attribute types nominally.
            return cast("MetricsBackend", OTELBaldurMetrics(prefix=prefix))
    except Exception:
        pass
    return BaldurMetrics(prefix=prefix)


from baldur.utils.singleton import make_singleton_factory

get_metrics, configure_metrics, reset_metrics = make_singleton_factory(
    "metrics", _create_metrics
)


# =============================================================================
# Convenience Functions (with domain cardinality guard)
# =============================================================================


def record_dlq_item_created(domain: str, failure_type: str) -> None:
    """Record that a new DLQ item was created."""
    from baldur.metrics.registry import resolve_domain_label

    get_metrics().record_dlq_item_created(resolve_domain_label(domain), failure_type)


def record_retry_attempt(domain: str, attempt_count: int, outcome: str) -> None:
    """Record a retry attempt outcome."""
    from baldur.metrics.registry import resolve_domain_label

    get_metrics().record_retry_attempt(
        resolve_domain_label(domain), attempt_count, outcome
    )


def record_recovery_time(
    domain: str,
    resolution_type: str,
    created_at: datetime,
    resolved_at: datetime,
) -> None:
    """Record time from failure to resolution."""
    from baldur.metrics.registry import resolve_domain_label

    get_metrics().record_recovery_time(
        resolve_domain_label(domain), resolution_type, created_at, resolved_at
    )


def record_sla_breach(domain: str) -> None:
    """Record an SLA breach event."""
    from baldur.metrics.registry import resolve_domain_label

    get_metrics().record_sla_breach(resolve_domain_label(domain))


def record_circuit_breaker_state_change(
    service_name: str,
    from_state: str,
    to_state: str,
) -> None:
    """Record a circuit breaker state transition.

    Composite Key (``service::cell_id``) is auto-split so the ``cell_id``
    label is set correctly.
    """
    from baldur.core.cb_namespace import (
        parse_composite_cb_name,
    )

    base_service, cell_id = parse_composite_cb_name(service_name)
    get_metrics().record_circuit_breaker_state_change(
        base_service, from_state, to_state, cell_id=cell_id
    )


def record_circuit_breaker_open_duration(
    service_name: str, duration_seconds: float
) -> None:
    """Record how long a circuit breaker was in open state."""
    get_metrics().record_circuit_breaker_open_duration(service_name, duration_seconds)


def record_replay_attempt(domain: str, replay_type: str, success: bool) -> None:
    """Record a replay attempt."""
    from baldur.metrics.registry import resolve_domain_label

    get_metrics().record_replay_attempt(
        resolve_domain_label(domain), replay_type, success
    )


# =============================================================================
# RED Metrics Convenience Functions
# =============================================================================


def record_http_request(
    method: str,
    endpoint: str,
    status_code: int,
    duration_seconds: float,
) -> None:
    """Record an HTTP request (Rate + Duration)."""
    get_metrics().record_http_request(method, endpoint, status_code, duration_seconds)


def record_http_error(method: str, endpoint: str, error_type: str) -> None:
    """Record an HTTP request error (Errors)."""
    get_metrics().record_http_error(method, endpoint, error_type)


# =============================================================================
# Four Golden Signals Convenience Functions
# =============================================================================


def set_request_queue_depth(service: str, depth: int) -> None:
    """Set current request queue depth (Saturation)."""
    get_metrics().set_request_queue_depth(service, depth)


def set_worker_utilization(pool_name: str, ratio: float) -> None:
    """Set worker pool utilization ratio (Saturation)."""
    get_metrics().set_worker_utilization(pool_name, ratio)


def set_active_connections(connection_type: str, count: int) -> None:
    """Set number of active connections (Saturation)."""
    get_metrics().set_active_connections(connection_type, count)


def set_latency_percentile(
    endpoint: str, percentile: str, value_seconds: float
) -> None:
    """Set request latency percentile (Latency)."""
    get_metrics().set_latency_percentile(endpoint, percentile, value_seconds)


def set_error_rate(service: str, rate_percent: float) -> None:
    """Set current error rate percentage (Errors)."""
    get_metrics().set_error_rate(service, rate_percent)
