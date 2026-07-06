"""
OTEL Meter-based metrics backend for BaldurMetrics.

This backend bridges OTEL instruments (for the natively-ported retry / replay /
infra recorders) AND reused Prometheus recorders (for every other family) into
the shared ``prometheus_client`` REGISTRY, which ``PrometheusMetricReader``
(initialized in ``observability/__init__.py``) exposes for /metrics scraping.
Both write to the one REGISTRY, so a series populates identically regardless of
which recorder shape backs it.

The collector-side aggregation benefit (delegating multiprocess aggregation to
the OTEL SDK / OTEL Collector) is NOT realized under the current pipeline, which
wires only a per-process scrape-model ``PrometheusMetricReader`` — there is no
OTLP ``PeriodicExportingMetricReader`` (push). That benefit is gated on the
unwired OTLP-push port; until then the OTel-native recorders are functionally
equivalent to the reused Prometheus recorders, rendering numerically-equivalent
prometheus exposition (the reused families share one recorder, so their text is
byte-identical; the native histograms reach identical metric/label/bucket-boundary
*values*, but the OTel exporter renders an integer bucket boundary as ``le="N"``
where prometheus_client renders ``le="N.0"`` — equal under ``histogram_quantile``).
Histogram-bucket parity specifically is backed by passing each native
``create_histogram`` an ``explicit_bucket_boundaries_advisory`` mirroring its
Prometheus recorder's ``buckets=`` tuple — without it the OTel SDK applies its
default (millisecond-scale) boundaries, which mis-render the seconds-valued
duration histograms under ``histogram_quantile``. Bucket-boundary parity between
the two backends is enforced by the G47 fitness function.

Recorder structure mirrors metrics/recorders/ for the Prometheus backend,
ensuring MetricsBackend Protocol compatibility.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from datetime import datetime

import structlog

from baldur.metrics.safe_gauge import clamp_percentage
from baldur.utils.time import utc_now

logger = structlog.get_logger()


class _GaugeStore:
    """Thread-safe value store for ObservableGauge callbacks."""

    def __init__(self):
        self._lock = threading.Lock()
        self._values: dict[tuple, float] = {}

    def set(self, value: float, attributes: dict | None = None) -> None:
        key = tuple(sorted((attributes or {}).items()))
        with self._lock:
            self._values[key] = value

    def inc(self, amount: float = 1, attributes: dict | None = None) -> None:
        """Increment the stored value by ``amount`` (atomic read-modify-write).

        Mirrors prometheus ``Gauge.inc`` semantics so the event-driven fast-path
        can mutate the observable gauge's source by +amount. Does NOT clamp —
        clamping is ``SafeGauge``'s job (parity with the raw prometheus Gauge,
        which can go negative).
        """
        key = tuple(sorted((attributes or {}).items()))
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) + amount

    def dec(self, amount: float = 1, attributes: dict | None = None) -> None:
        """Decrement the stored value by ``amount`` (atomic read-modify-write).

        Counterpart to :meth:`inc`; does NOT clamp at 0 (see :meth:`inc`).
        """
        key = tuple(sorted((attributes or {}).items()))
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) - amount

    def get(self, attributes: dict | None = None) -> float:
        """Read the stored value for ``attributes`` (0.0 if never set).

        Sibling read-accessor to :meth:`set`/:meth:`inc`/:meth:`dec` — the
        drift-before snapshot reads the in-memory gauge through this. A
        never-set key returns 0.0 so an unhydrated gauge reads as a real 0
        (treated as drift against a non-zero data source, not masked away).
        """
        key = tuple(sorted((attributes or {}).items()))
        with self._lock:
            return self._values.get(key, 0.0)

    def callback(self, options):
        from opentelemetry.metrics import Observation

        with self._lock:
            results = []
            for attr_key, value in self._values.items():
                attrs = dict(attr_key)
                results.append(Observation(value, attrs))
            return results


# =============================================================================
# OTEL Recorder wrappers — same method signatures as Prometheus recorders
# =============================================================================


class _OTELRetryRecorder:
    """OTEL Retry/Recovery metric recorder."""

    def __init__(self, meter, prefix: str, gauge_store_fn):
        self._attempts_histogram = meter.create_histogram(
            f"{prefix}_retry_attempts_distribution",
            description="Number of retry attempts before resolution",
            # Mirror RetryMetricRecorder._attempts_histogram buckets (G47 parity).
            explicit_bucket_boundaries_advisory=[1, 2, 3, 4, 5, 6, 7, 8, 9, 10],
        )
        self._outcomes_total = meter.create_counter(
            f"{prefix}_retry_outcomes_total",
            description="Retry outcomes by domain and result",
        )
        self._success_store = gauge_store_fn("retry_success")
        meter.create_observable_gauge(
            f"{prefix}_retry_success_rate",
            callbacks=[self._success_store.callback],
            description="Percentage of successful retries (0-100)",
        )
        self._delay_seconds = meter.create_histogram(
            f"{prefix}_retry_delay_seconds",
            description="Retry delay in seconds",
            # Mirror RetryMetricRecorder._delay_seconds buckets (G47 parity).
            explicit_bucket_boundaries_advisory=[1, 5, 10, 30, 60, 120, 300, 600],
        )
        self._recovery_time = meter.create_histogram(
            f"{prefix}_recovery_time_seconds",
            description="Time from failure to resolution in seconds",
            # Mirror RetryMetricRecorder._recovery_time_seconds buckets (G47 parity).
            explicit_bucket_boundaries_advisory=[
                60,
                300,
                900,
                1800,
                3600,
                7200,
                14400,
                28800,
                86400,
            ],
        )
        self._sla_breach_total = meter.create_counter(
            f"{prefix}_sla_breach_total",
            description="Total SLA breaches detected",
        )

    def record_attempt(self, domain: str, attempt_count: int, outcome: str) -> None:
        try:
            from baldur.core.test_mode_context import TestModeContext

            is_synthetic = TestModeContext.get_synthetic_label_value()
            self._attempts_histogram.record(
                attempt_count, {"domain": domain, "is_synthetic": is_synthetic}
            )
            self._outcomes_total.add(
                1,
                {"domain": domain, "outcome": outcome, "is_synthetic": is_synthetic},
            )
        except Exception as e:
            logger.warning("metrics.record_retry_metric_failed", error=e)

    def record_retry(
        self, domain: str, success: bool, delay: float | None = None
    ) -> None:
        try:
            from baldur.core.test_mode_context import TestModeContext

            is_synthetic = TestModeContext.get_synthetic_label_value()
            outcome = "success" if success else "failure"
            self._outcomes_total.add(
                1,
                {"domain": domain, "outcome": outcome, "is_synthetic": is_synthetic},
            )
            if delay is not None:
                self._delay_seconds.record(delay, {"domain": domain})
        except Exception as e:
            logger.warning("metrics.record_retry_metric_failed", error=e)

    def set_success_rate(self, domain: str, rate: float) -> None:
        safe_rate = clamp_percentage(rate, f"retry_success_rate[{domain}]")
        self._success_store.set(safe_rate, {"domain": domain})

    def record_recovery_duration(
        self,
        domain: str,
        resolution_type: str,
        duration_seconds: float,
    ) -> None:
        try:
            self._recovery_time.record(
                duration_seconds,
                {"domain": domain, "resolution_type": resolution_type},
            )
        except Exception as e:
            logger.warning("metrics.record_recovery_time_failed", error=e)

    def record_recovery_time(
        self,
        domain: str,
        resolution_type: str,
        created_at: datetime,
        resolved_at: datetime,
    ) -> None:
        duration = (resolved_at - created_at).total_seconds()
        self.record_recovery_duration(domain, resolution_type, duration)

    def record_sla_breach(self, domain: str) -> None:
        try:
            self._sla_breach_total.add(1, {"domain": domain})
        except Exception as e:
            logger.warning("metrics.record_sla_breach_failed", error=e)


class _OTELReplayRecorder:
    """OTEL Replay metric recorder."""

    def __init__(self, meter, prefix: str):
        self._attempts_total = meter.create_counter(
            f"{prefix}_replay_attempts_total",
            description="Total replay attempts",
        )
        self._outcomes_total = meter.create_counter(
            f"{prefix}_replay_outcomes_total",
            description="Replay outcomes",
        )
        self._duration_seconds = meter.create_histogram(
            f"{prefix}_replay_duration_seconds",
            description="Replay operation duration",
            # Mirror ReplayMetricRecorder._duration_seconds buckets (G47 parity).
            explicit_bucket_boundaries_advisory=[0.1, 0.5, 1, 2, 5, 10, 30],
        )

    def record_started(self, domain: str, replay_type: str) -> None:
        try:
            from baldur.core.test_mode_context import TestModeContext

            is_synthetic = TestModeContext.get_synthetic_label_value()
            self._attempts_total.add(
                1,
                {
                    "domain": domain,
                    "replay_type": replay_type,
                    "is_synthetic": is_synthetic,
                },
            )
        except Exception as e:
            logger.warning("metrics.record_replay_metric_failed", error=e)

    def record_attempt(self, domain: str, replay_type: str, success: bool) -> None:
        try:
            from baldur.core.test_mode_context import TestModeContext

            is_synthetic = TestModeContext.get_synthetic_label_value()
            self._attempts_total.add(
                1,
                {
                    "domain": domain,
                    "replay_type": replay_type,
                    "is_synthetic": is_synthetic,
                },
            )
            outcome = "success" if success else "failure"
            self._outcomes_total.add(
                1,
                {"domain": domain, "outcome": outcome, "is_synthetic": is_synthetic},
            )
        except Exception as e:
            logger.warning("metrics.record_replay_metric_failed", error=e)

    def record_replay(
        self, domain: str, result: str, duration: float | None = None
    ) -> None:
        try:
            from baldur.core.test_mode_context import TestModeContext

            is_synthetic = TestModeContext.get_synthetic_label_value()
            self._outcomes_total.add(
                1,
                {"domain": domain, "outcome": result, "is_synthetic": is_synthetic},
            )
            if duration is not None:
                self._duration_seconds.record(duration, {"domain": domain})
        except Exception as e:
            logger.warning("metrics.record_replay_metric_failed", error=e)


class _OTELInfraRecorder:
    """OTEL Infrastructure metric recorder."""

    def __init__(self, meter, prefix: str, gauge_store_fn):
        # RED Metrics
        self._http_requests_total = meter.create_counter(
            f"{prefix}_http_requests_total",
            description="Total HTTP requests (Rate)",
        )
        self._http_duration = meter.create_histogram(
            f"{prefix}_http_request_duration_seconds",
            description="HTTP request duration in seconds (Duration)",
            # Mirror InfraMetricRecorder._http_request_duration buckets (G47 parity).
            explicit_bucket_boundaries_advisory=[
                0.005,
                0.01,
                0.025,
                0.05,
                0.1,
                0.25,
                0.5,
                1.0,
                2.5,
                5.0,
                10.0,
            ],
        )
        self._http_errors_total = meter.create_counter(
            f"{prefix}_http_request_errors_total",
            description="Total HTTP request errors (Errors)",
        )

        # Saturation
        self._queue_depth_store = gauge_store_fn("queue_depth")
        meter.create_observable_gauge(
            f"{prefix}_request_queue_depth",
            callbacks=[self._queue_depth_store.callback],
            description="Current request queue depth (Saturation)",
        )
        self._worker_util_store = gauge_store_fn("worker_util")
        meter.create_observable_gauge(
            f"{prefix}_worker_utilization_ratio",
            callbacks=[self._worker_util_store.callback],
            description="Worker pool utilization ratio 0.0-1.0 (Saturation)",
        )
        self._active_conn_store = gauge_store_fn("active_conn")
        meter.create_observable_gauge(
            f"{prefix}_active_connections",
            callbacks=[self._active_conn_store.callback],
            description="Number of active connections (Saturation)",
        )
        self._latency_pct_store = gauge_store_fn("latency_pct")
        meter.create_observable_gauge(
            f"{prefix}_request_latency_percentile_seconds",
            callbacks=[self._latency_pct_store.callback],
            description="Request latency percentiles (Latency)",
        )
        self._error_rate_store = gauge_store_fn("error_rate")
        meter.create_observable_gauge(
            f"{prefix}_error_rate_percent",
            callbacks=[self._error_rate_store.callback],
            description="Current error rate percentage (Errors)",
        )

        # Security
        self._security_incidents = meter.create_counter(
            f"{prefix}_security_incidents_total",
            description="Total security incidents",
        )

        # Mesh Coordinator
        self._mesh_overrides_store = gauge_store_fn("mesh_overrides")
        meter.create_observable_gauge(
            f"{prefix}_mesh_overrides_active",
            callbacks=[self._mesh_overrides_store.callback],
            description="Current active mesh threshold overrides",
        )
        self._mesh_override_applied_total = meter.create_counter(
            f"{prefix}_mesh_override_applied_total",
            description="Total mesh threshold overrides applied",
        )
        self._mesh_override_released_total = meter.create_counter(
            f"{prefix}_mesh_override_released_total",
            description="Total mesh threshold overrides released",
        )
        self._mesh_override_expired_total = meter.create_counter(
            f"{prefix}_mesh_override_expired_total",
            description="Total mesh threshold overrides expired by TTL",
        )
        self._mesh_override_renewed_total = meter.create_counter(
            f"{prefix}_mesh_override_renewed_total",
            description="Total mesh threshold override TTL renewals",
        )

        # DI Fallback
        self._di_fallback_total = meter.create_counter(
            f"{prefix}_di_fallback_total",
            description="DI fallback to in-memory adapter",
        )

        # Capacity
        self._capacity_warmup_total = meter.create_counter(
            f"{prefix}_capacity_warmup_total",
            description="Total warm-up executions",
        )
        self._capacity_warmup_duration = meter.create_histogram(
            f"{prefix}_capacity_warmup_duration_seconds",
            description="Warm-up execution duration in seconds",
            # Mirror InfraMetricRecorder._capacity_warmup_duration buckets (G47 parity).
            explicit_bucket_boundaries_advisory=[0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0],
        )
        self._capacity_cooldown_total = meter.create_counter(
            f"{prefix}_capacity_cooldown_total",
            description="Total cool-down executions",
        )
        self._capacity_events_store = gauge_store_fn("capacity_events")
        meter.create_observable_gauge(
            f"{prefix}_capacity_active_events",
            callbacks=[self._capacity_events_store.callback],
            description="Currently active scheduled events",
        )
        self._capacity_rate_store = gauge_store_fn("capacity_rate")
        meter.create_observable_gauge(
            f"{prefix}_capacity_rate_multiplier",
            callbacks=[self._capacity_rate_store.callback],
            description="Currently applied rate multiplier",
        )
        self._capacity_pool_store = gauge_store_fn("capacity_pool")
        meter.create_observable_gauge(
            f"{prefix}_capacity_pool_multiplier",
            callbacks=[self._capacity_pool_store.callback],
            description="Currently applied pool multiplier",
        )

    def record_http_request(
        self,
        method: str,
        endpoint: str,
        status_code: int,
        duration_seconds: float,
    ) -> None:
        self._http_requests_total.add(
            1,
            {"method": method, "endpoint": endpoint, "status_code": str(status_code)},
        )
        self._http_duration.record(
            duration_seconds, {"method": method, "endpoint": endpoint}
        )

    def record_http_error(self, method: str, endpoint: str, error_type: str) -> None:
        self._http_errors_total.add(
            1, {"method": method, "endpoint": endpoint, "error_type": error_type}
        )

    @contextmanager
    def http_request_timer(self, method: str, endpoint: str):
        start_time = utc_now()
        error_occurred = False
        error_type = None
        try:
            yield
        except Exception as e:
            error_occurred = True
            error_type = type(e).__name__
            raise
        finally:
            duration = (utc_now() - start_time).total_seconds()
            self._http_duration.record(
                duration, {"method": method, "endpoint": endpoint}
            )
            if error_occurred and error_type:
                self.record_http_error(method, endpoint, error_type)

    def set_request_queue_depth(self, service: str, depth: int) -> None:
        self._queue_depth_store.set(depth, {"service": service})

    def set_worker_utilization(self, pool_name: str, ratio: float) -> None:
        self._worker_util_store.set(ratio, {"pool_name": pool_name})

    def set_active_connections(self, connection_type: str, count: int) -> None:
        self._active_conn_store.set(count, {"connection_type": connection_type})

    def set_latency_percentile(
        self, endpoint: str, percentile: str, value_seconds: float
    ) -> None:
        self._latency_pct_store.set(
            value_seconds, {"percentile": percentile, "endpoint": endpoint}
        )

    def set_error_rate(self, service: str, rate_percent: float) -> None:
        safe_rate = clamp_percentage(rate_percent, f"error_rate[{service}]")
        self._error_rate_store.set(safe_rate, {"service": service})

    def set_info(self, info_dict: dict[str, str]) -> None:
        pass  # OTEL uses Resource attributes, not Info metric

    def record_security_incident(self, incident_type: str, severity: str) -> None:
        self._security_incidents.add(
            1, {"incident_type": incident_type, "severity": severity}
        )

    def set_mesh_overrides_active(self, count: int) -> None:
        self._mesh_overrides_store.set(count)

    def record_mesh_override_applied(self) -> None:
        self._mesh_override_applied_total.add(1)

    def record_mesh_override_released(self) -> None:
        self._mesh_override_released_total.add(1)

    def record_mesh_override_expired(self) -> None:
        self._mesh_override_expired_total.add(1)

    def record_mesh_override_renewed(self) -> None:
        self._mesh_override_renewed_total.add(1)

    def record_di_fallback(self, service: str, adapter: str) -> None:
        self._di_fallback_total.add(1, {"service": service, "adapter": adapter})

    def record_capacity_warmup(self, event_id: str, outcome: str) -> None:
        self._capacity_warmup_total.add(1, {"event_id": event_id, "outcome": outcome})

    def record_capacity_cooldown(self, event_id: str, outcome: str) -> None:
        self._capacity_cooldown_total.add(1, {"event_id": event_id, "outcome": outcome})

    def set_capacity_active_events(self, count: int) -> None:
        self._capacity_events_store.set(count)

    def set_capacity_rate_multiplier(self, value: float) -> None:
        self._capacity_rate_store.set(value)

    def set_capacity_pool_multiplier(self, value: float) -> None:
        self._capacity_pool_store.set(value)


class OTELBaldurMetrics:
    """OTEL Meter-based implementation of BaldurMetrics.

    Provides the same public API as BaldurMetrics (prometheus.py)
    and satisfies the MetricsBackend Protocol. Uses OTEL instruments
    internally. PrometheusMetricReader converts them to Prometheus text
    format for /metrics endpoint.
    """

    def __init__(self, prefix: str = "baldur"):
        self.prefix = prefix
        self._initialized = False
        self._gauge_stores: dict[str, _GaugeStore] = {}

        try:
            from baldur.observability import get_meter

            meter = get_meter()
            if meter is None:
                logger.warning("otel_metrics.meter_not_available")
                return

            def gauge_store_fn(name: str) -> _GaugeStore:
                if name not in self._gauge_stores:
                    self._gauge_stores[name] = _GaugeStore()
                return self._gauge_stores[name]

            # OTel-native recorders — retry / replay / infra are method-complete
            # native ports backed by OTel instruments + observable-gauge stores.
            self.retry = _OTELRetryRecorder(meter, prefix, gauge_store_fn)
            self.replay = _OTELReplayRecorder(meter, prefix)
            self.infra = _OTELInfraRecorder(meter, prefix, gauge_store_fn)

            # Every other family reuses its Prometheus recorder (shared REGISTRY).
            self._init_reused_prometheus_recorders()
            self._initialized = True

        except Exception as e:
            logger.warning("otel_metrics.initialization_failed", error=e)

    def _init_reused_prometheus_recorders(self) -> None:
        """Instantiate every family that reuses its Prometheus recorder.

        These write to the shared ``prometheus_client`` REGISTRY via
        ``get_or_create_*``, which ``generate_latest()`` exposes alongside the
        bridged OTel instruments, so each series populates identically under
        either backend. Reuse is functionally equivalent to a native port under
        the scrape-only ``PrometheusMetricReader`` pipeline (see the module
        docstring), and it gives cb/dlq full method coverage automatically — the
        former native ``_OTELCBRecorder`` / ``_OTELDLQRecorder`` were incomplete
        ports that silently dropped ``record_blocked`` /
        ``record_acquire_duration`` / ... on the OTel backend. Explicit
        instantiation (not ``__getattr__``) keeps every family a real,
        introspectable attribute (the G46 parity gate compares attribute sets
        directly). All constructors are zero-arg and idempotent
        (``get_or_create_*`` double-register is a no-op).
        """
        from baldur.metrics.recorders import (
            AutoTuningMetricRecorder,
            CanaryMetricRecorder,
            CBMetricRecorder,
            CorrelationEngineMetricRecorder,
            CorruptionShieldMetricRecorder,
            DailyReportMetricRecorder,
            DLQMetricRecorder,
            EmergencyModeMetricRecorder,
            EventBusMetricRecorder,
            ForecasterMetricRecorder,
            GovernanceMetricRecorder,
            HealthCheckMetricRecorder,
            HedgingMetricRecorder,
            IdempotencyMetricRecorder,
            LearningMetricRecorder,
            NotificationMetricRecorder,
            PoolMetricRecorder,
            PostmortemMetricRecorder,
            RecommendationMetricRecorder,
            RuntimeConfigMetricRecorder,
            ShutdownMetricRecorder,
            SystemControlMetricRecorder,
            ThrottleMetricRecorder,
            WatchdogMetricRecorder,
        )

        # Not re-exported by recorders/__init__ — import from their own modules.
        from baldur.metrics.recorders.bulkhead import BulkheadMetricRecorder
        from baldur.metrics.recorders.daemon_worker import DaemonWorkerMetricRecorder
        from baldur.metrics.recorders.entitlement import EntitlementMetricRecorder
        from baldur.metrics.recorders.executor import ExecutorMetricRecorder
        from baldur.metrics.recorders.protect import ProtectMetricRecorder

        self.dlq = DLQMetricRecorder()
        self.circuit_breaker = CBMetricRecorder()
        self.throttle = ThrottleMetricRecorder()
        self.bulkhead = BulkheadMetricRecorder()
        self.correlation_engine = CorrelationEngineMetricRecorder()
        self.auto_tuning = AutoTuningMetricRecorder()
        self.recommendation = RecommendationMetricRecorder()
        self.health_check = HealthCheckMetricRecorder()
        self.shutdown = ShutdownMetricRecorder()
        self.system_control = SystemControlMetricRecorder()
        self.emergency_mode = EmergencyModeMetricRecorder()
        self.event_bus = EventBusMetricRecorder()
        self.hedging = HedgingMetricRecorder()
        self.pool_monitor = PoolMetricRecorder()
        self.canary = CanaryMetricRecorder()
        self.runtime_config = RuntimeConfigMetricRecorder()
        self.corruption_shield = CorruptionShieldMetricRecorder()
        self.learning = LearningMetricRecorder()
        self.forecaster = ForecasterMetricRecorder()
        self.daily_report = DailyReportMetricRecorder()
        self.watchdog = WatchdogMetricRecorder()
        self.notification = NotificationMetricRecorder()
        self.postmortem = PostmortemMetricRecorder()
        self.governance = GovernanceMetricRecorder()
        self.entitlement = EntitlementMetricRecorder()
        # `protect` is not a getattr->None victim — baldur.protect() records via
        # the module-level get_protect_recorder() singleton, not via
        # get_metrics().protect. This instance exists purely for G46 family
        # parity; both ProtectMetricRecorder instances back the same prometheus
        # series via get_or_create_* (double-count-safe).
        self.protect = ProtectMetricRecorder()
        self.idempotency = IdempotencyMetricRecorder()
        self.executor = ExecutorMetricRecorder()
        self.daemon_workers = DaemonWorkerMetricRecorder()

    # =========================================================================
    # Backward-compatible delegate methods (same as Prometheus facade)
    # =========================================================================

    # --- DLQ ---
    def record_dlq_item_created(self, domain: str, failure_type: str) -> None:
        if not self._initialized:
            return
        self.dlq.record_item_created(domain, failure_type)

    def set_dlq_pending_count(self, domain: str, count: int) -> None:
        if not self._initialized:
            return
        self.dlq.set_pending_count(domain, count)

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

    # --- HTTP / RED ---
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

    # --- Saturation ---
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
        start_time = utc_now()
        try:
            yield
        finally:
            duration = (utc_now() - start_time).total_seconds()
            if metric_type == "replay":
                self.replay.record_replay(domain, "timed", duration)

    # --- Mesh ---
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
