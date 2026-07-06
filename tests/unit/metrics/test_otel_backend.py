"""Unit tests for metrics/otel_backend.py — OTEL Meter-based metrics.

Tests _GaugeStore thread-safety and OTELBaldurMetrics initialization
and recording methods added in commit cf89883a.

Reference:
    docs/baldur/middleware_system/316_GUNICORN_PRELOAD_OPTIMIZATION.md §5.8
"""

from __future__ import annotations

import re
import threading
from unittest.mock import MagicMock, patch

import pytest

from baldur.metrics.otel_backend import OTELBaldurMetrics, _GaugeStore


def _exposition() -> str:
    """Current prometheus_client exposition text from the shared REGISTRY.

    The reused Prometheus recorders write here, so a recorded series surfaces in
    this text regardless of which metrics backend wrote it (648 D1/D2).
    """
    from prometheus_client import generate_latest

    return generate_latest().decode()


@pytest.fixture
def otel_metrics():
    """An initialized OTel backend backed by a stub meter.

    648 D1/D2: every family except the native ``retry``/``replay``/``infra``
    recorders reuses its Prometheus recorder, which writes to the shared
    ``prometheus_client`` REGISTRY and never touches the OTEL meter. A MagicMock
    meter therefore drives ``_initialized=True`` and fully exercises the
    family/method/facade recording paths (Test Assessment: MagicMock meter for
    the prometheus-reused families). The patch only needs to be active during
    ``__init__`` — recording does not re-resolve the meter.
    """
    with patch("baldur.observability.get_meter", return_value=MagicMock()):
        yield OTELBaldurMetrics()


class TestGaugeStoreContract:
    """Contract: _GaugeStore stores values keyed by sorted attribute tuples."""

    def test_set_without_attributes_uses_empty_key(self):
        """set(value) with no attributes uses empty tuple as key."""
        store = _GaugeStore()
        store.set(42.0)
        assert store._values[()] == 42.0

    def test_set_with_attributes_sorts_keys(self):
        """Attributes are sorted by key for deterministic lookup."""
        store = _GaugeStore()
        store.set(10.0, {"z_key": "z", "a_key": "a"})
        expected_key = (("a_key", "a"), ("z_key", "z"))
        assert expected_key in store._values

    def test_overwrite_same_key(self):
        """Setting same attributes twice overwrites the value."""
        store = _GaugeStore()
        store.set(1.0, {"service": "api"})
        store.set(2.0, {"service": "api"})
        key = (("service", "api"),)
        assert store._values[key] == 2.0


class TestGaugeStoreCallbackBehavior:
    """Behavior: callback returns Observation list for OTEL SDK."""

    def test_callback_returns_observations_for_all_stored_values(self):
        """callback() returns one Observation per stored key."""
        store = _GaugeStore()
        store.set(1.0, {"a": "1"})
        store.set(2.0, {"b": "2"})

        mock_observation = MagicMock()
        with patch("opentelemetry.metrics.Observation", mock_observation):
            results = store.callback(options=None)

        assert len(results) == 2
        assert mock_observation.call_count == 2

    def test_callback_empty_store_returns_empty_list(self):
        """Empty store → empty observation list."""
        store = _GaugeStore()
        with patch("opentelemetry.metrics.Observation", MagicMock()):
            results = store.callback(options=None)
        assert results == []


class TestGaugeStoreThreadSafetyBehavior:
    """Behavior: concurrent set() calls do not corrupt data."""

    def test_concurrent_set_no_data_corruption(self):
        """10 threads writing simultaneously produce no errors."""
        store = _GaugeStore()
        errors = []

        def worker(thread_id):
            try:
                for i in range(100):
                    store.set(float(i), {"thread": str(thread_id)})
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        assert len(store._values) == 10


class TestOTELBaldurMetricsInitBehavior:
    """Behavior: initialization with/without OTEL meter."""

    @patch("baldur.observability.get_meter", return_value=None)
    def test_uninitialized_when_meter_is_none(self, _):
        """When get_meter() returns None, _initialized stays False."""
        metrics = OTELBaldurMetrics()
        assert metrics._initialized is False

    @patch("baldur.observability.get_meter", side_effect=ImportError)
    def test_uninitialized_on_import_error(self, _):
        """Import error during init → _initialized stays False, no crash."""
        metrics = OTELBaldurMetrics()
        assert metrics._initialized is False

    @patch("baldur.observability.get_meter")
    def test_initialized_when_meter_available(self, mock_get_meter):
        """When get_meter() returns a valid Meter, _initialized is True."""
        mock_meter = MagicMock()
        mock_get_meter.return_value = mock_meter

        metrics = OTELBaldurMetrics(prefix="test")

        assert metrics._initialized is True
        assert metrics.prefix == "test"
        assert mock_meter.create_counter.call_count > 0
        assert mock_meter.create_histogram.call_count > 0
        assert mock_meter.create_observable_gauge.call_count > 0


class TestOTELBaldurMetricsRecordingBehavior:
    """Behavior: recording methods are no-ops when uninitialized."""

    def _make_uninitialized_metrics(self):
        """Create an OTELBaldurMetrics that is not initialized."""
        with patch("baldur.observability.get_meter", return_value=None):
            return OTELBaldurMetrics()

    def test_record_dlq_item_created_noop_when_uninitialized(self):
        """Uninitialized metrics silently skip recording."""
        metrics = self._make_uninitialized_metrics()
        metrics.record_dlq_item_created("orders", "timeout")

    def test_record_retry_attempt_noop_when_uninitialized(self):
        """Uninitialized metrics silently skip retry recording."""
        metrics = self._make_uninitialized_metrics()
        metrics.record_retry_attempt("payments", 3, "success")

    def test_set_circuit_state_noop_when_uninitialized(self):
        """Uninitialized metrics silently skip circuit state."""
        metrics = self._make_uninitialized_metrics()
        metrics.set_circuit_state("payment_service", "open")

    @patch("baldur.observability.get_meter")
    def test_record_dlq_item_created_records_under_otel_backend(self, mock_get_meter):
        """648 D2: the OTel backend reuses the prometheus DLQ recorder, so a record
        surfaces in the shared prometheus_client exposition (was a silent no-op /
        OTel-native counter before the swap)."""
        from prometheus_client import generate_latest

        mock_get_meter.return_value = MagicMock()
        metrics = OTELBaldurMetrics()

        metrics.record_dlq_item_created("otel_dlq_probe", "timeout")

        output = generate_latest().decode()
        assert 'baldur_dlq_created_total{domain="otel_dlq_probe"}' in output

    @patch("baldur.observability.get_meter")
    def test_set_circuit_state_records_under_otel_backend(self, mock_get_meter):
        """648 D2: cb is the prometheus CBMetricRecorder; set_circuit_state exposes
        baldur_circuit_breaker_state in the shared exposition (state string →
        numeric: closed=0, open=1, half_open=2)."""
        from prometheus_client import generate_latest

        mock_get_meter.return_value = MagicMock()
        metrics = OTELBaldurMetrics()

        metrics.set_circuit_state("otel_cb_probe", "open", "cell-0")

        output = generate_latest().decode()
        # prometheus_client renders labels sorted: cell_id before service (D15)
        assert (
            'baldur_circuit_breaker_state{cell_id="cell-0",service="otel_cb_probe"} 1.0'
            in output
        )


class TestOTELBaldurMetricsTimerBehavior:
    """Behavior: context manager timer records duration."""

    @patch("baldur.observability.get_meter")
    def test_timer_records_replay_duration(self, mock_get_meter):
        """timer() context manager records duration to replay histogram."""
        histograms = {}

        def make_histogram(name, **_kw):
            mock = MagicMock(name=f"hist_{name}")
            histograms[name] = mock
            return mock

        mock_meter = MagicMock()
        mock_meter.create_histogram.side_effect = make_histogram
        mock_get_meter.return_value = mock_meter
        metrics = OTELBaldurMetrics()

        with metrics.timer("orders", "replay"):
            pass

        metrics.replay._outcomes_total.add.assert_called_once()
        call_args = metrics.replay._outcomes_total.add.call_args
        assert call_args[0][1]["domain"] == "orders"

    @patch("baldur.observability.get_meter")
    def test_http_request_timer_records_on_error(self, mock_get_meter):
        """http_request_timer records duration and error type on exception."""
        counters = {}
        histograms = {}

        def make_counter(name, **_kw):
            mock = MagicMock(name=f"counter_{name}")
            counters[name] = mock
            return mock

        def make_histogram(name, **_kw):
            mock = MagicMock(name=f"hist_{name}")
            histograms[name] = mock
            return mock

        mock_meter = MagicMock()
        mock_meter.create_counter.side_effect = make_counter
        mock_meter.create_histogram.side_effect = make_histogram
        mock_get_meter.return_value = mock_meter
        metrics = OTELBaldurMetrics()

        with pytest.raises(ValueError):
            with metrics.http_request_timer("GET", "/api/test"):
                raise ValueError("test error")

        metrics.infra._http_duration.record.assert_called_once()
        metrics.infra._http_errors_total.add.assert_called_once()
        error_call = metrics.infra._http_errors_total.add.call_args
        # add(1, {"method": ..., "endpoint": ..., "error_type": ...})
        error_attrs = error_call[0][1]
        assert error_attrs["error_type"] == "ValueError"


# =============================================================================
# 648 — backend recorder parity behavior coverage
# =============================================================================

# 648 D1: the 11 families that have live ``getattr(get_metrics(), "<family>",
# None)`` consumers (+ the PRO-v1.0 subset emergency_mode/watchdog/notification),
# which silently no-op'd on the OTel backend before the family gap was closed.
# Each entry records via the family attribute and asserts a uniquely-labelled
# series surfaces in the shared exposition. Label values are unique to this test
# so the assertion is deterministic under the shared, never-reset REGISTRY.
_D1_FAMILY_RECORDINGS = [
    pytest.param(
        lambda m: m.idempotency.record_check("miss", "otel_d1_idem"),
        'baldur_idempotency_check_total{domain="otel_d1_idem",result="miss"} 1.0',
        id="idempotency",
    ),
    pytest.param(
        lambda m: m.health_check.set_status("otel_d1_hc", "degraded"),
        'baldur_health_check_status{check_type="otel_d1_hc"} 1.0',
        id="health_check",
    ),
    pytest.param(
        lambda m: m.system_control.set_enabled(True),
        "baldur_system_control_enabled 1.0",
        id="system_control",
    ),
    pytest.param(
        lambda m: m.emergency_mode.set_level("level_2"),
        "baldur_emergency_mode_level 2.0",
        id="emergency_mode",
    ),
    pytest.param(
        lambda m: m.watchdog.record_probe("circuit_breaker", "otel_d1_probe"),
        'baldur_watchdog_probe_total{component="circuit_breaker",status="otel_d1_probe"} 1.0',
        id="watchdog",
    ),
    pytest.param(
        lambda m: m.notification.record_sent("otel_d1_chan", "low", "success"),
        'baldur_notification_sent_total{channel="otel_d1_chan",priority="low",result="success"} 1.0',
        id="notification",
    ),
    pytest.param(
        lambda m: m.governance.record_break_glass("otel_d1_reason"),
        'baldur_governance_break_glass_total{reason="otel_d1_reason"} 1.0',
        id="governance",
    ),
    pytest.param(
        lambda m: m.entitlement.set_status(2),
        "baldur_entitlement_status 2.0",
        id="entitlement",
    ),
    pytest.param(
        lambda m: m.event_bus.record_emit_skipped("otel_d1_src"),
        'baldur_eventbus_emit_skipped_total{source="otel_d1_src"} 1.0',
        id="event_bus",
    ),
    pytest.param(
        lambda m: m.postmortem.record_generated("otel_d1_pm"),
        'baldur_postmortem_generated_total{type="otel_d1_pm"} 1.0',
        id="postmortem",
    ),
    pytest.param(
        lambda m: m.shutdown.set_phase("draining"),
        "baldur_shutdown_phase 1.0",
        id="shutdown",
    ),
]


class TestOtelBackendFamilyRecording:
    """648 D1 — families reachable only via ``getattr(get_metrics(), ...)`` now
    record under the OTel backend.

    Before the family gap was closed these 25 families were absent from the OTel
    backend, so ``getattr(...)`` returned ``None``, the consumer's ``is not None``
    guard skipped, and the metric was silently never recorded (claim-wiring
    behaviorally-invisible). Each family now reuses its Prometheus recorder and
    writes to the shared REGISTRY, so the series surfaces in ``generate_latest()``
    under either backend.
    """

    @pytest.mark.parametrize(("record_fn", "expected_series"), _D1_FAMILY_RECORDINGS)
    def test_family_records_under_otel_backend(
        self, otel_metrics, record_fn, expected_series
    ):
        """A record call on the family attribute advances its series in the
        shared exposition (was a getattr->None silent no-op before 648 D1)."""
        # Given an initialized OTel backend (family present, not None)
        # When the family records once
        record_fn(otel_metrics)

        # Then the uniquely-labelled series appears in the shared exposition
        assert expected_series in _exposition()


def _register_and_record_dlq_acquire(metrics) -> None:
    """Register a unique domain then record an acquire duration on it.

    ``record_acquire_duration`` runs the domain through the cardinality guard
    (``resolve_domain_label``), which collapses unregistered domains to the
    fallback label. Registering the probe domain keeps the asserted label
    deterministic and unique to this test.
    """
    from baldur.metrics.registry import register_domain

    register_domain("otel_d2_acq")
    metrics.dlq.record_acquire_duration("otel_d2_acq", 0.01)


# 648 D2: the methods the incomplete native ports (_OTELCBRecorder /
# _OTELDLQRecorder) dropped — present-family consumers called them and the
# resulting AttributeError was swallowed by the fail-open guard. After swapping
# cb/dlq to the full Prometheus recorders the calls land in the exposition.
_D2_METHOD_DROP_CLOSURES = [
    pytest.param(
        lambda m: m.circuit_breaker.record_blocked("otel_d2_cb", "open"),
        'baldur_circuit_breaker_blocked_total{reason="open",service="otel_d2_cb"} 1.0',
        id="cb_record_blocked",
    ),
    pytest.param(
        _register_and_record_dlq_acquire,
        'baldur_dlq_acquire_duration_seconds_count{domain="otel_d2_acq"} 1.0',
        id="dlq_record_acquire_duration",
    ),
]


class TestOtelBackendMethodDropClosure:
    """648 D2 — methods missing from the incomplete native cb/dlq ports now exist
    and record under the OTel backend.

    These are the confirmed live silent drops: ``record_blocked`` ->
    ``baldur_circuit_breaker_blocked_total`` and ``record_acquire_duration`` ->
    ``baldur_dlq_acquire_duration_*``. The family-level ``hasattr`` guard passed
    (the family existed) while the method was absent, so the call raised an
    AttributeError that the caller's fail-open envelope swallowed.
    """

    @pytest.mark.parametrize(("record_fn", "expected_series"), _D2_METHOD_DROP_CLOSURES)
    def test_dropped_method_records_under_otel_backend(
        self, otel_metrics, record_fn, expected_series
    ):
        """Calling the previously-missing method advances its series instead of
        raising an AttributeError swallowed by the fail-open guard."""
        record_fn(otel_metrics)

        assert expected_series in _exposition()


class TestOtelBackendFacadeMethod:
    """648 D3 — the ``record_dlq_domain_input_rejected`` facade delegate, the only
    facade method present on BaldurMetrics but absent on the OTel backend."""

    def test_facade_method_is_present_on_class(self):
        """Contract: the delegate exists on OTELBaldurMetrics (facade parity)."""
        assert hasattr(OTELBaldurMetrics, "record_dlq_domain_input_rejected")

    def test_facade_method_records_under_otel_backend(self, otel_metrics):
        """Behavior: the facade delegate advances the dlq domain-rejected counter
        in the shared exposition."""
        otel_metrics.record_dlq_domain_input_rejected("otel_d3_site")

        assert (
            'baldur_dlq_domain_input_rejected_total{site="otel_d3_site"} 1.0'
            in _exposition()
        )

    def test_facade_method_noop_when_uninitialized(self):
        """The delegate is a no-op (no crash) when the backend never initialized,
        matching every other facade delegate's ``_initialized`` guard."""
        with patch("baldur.observability.get_meter", return_value=None):
            metrics = OTELBaldurMetrics()
        assert metrics._initialized is False

        metrics.record_dlq_domain_input_rejected("otel_d3_uninit")


class TestOtelCbStateRegression:
    """648 D5 — the CB-state gauge exports under the OTel backend after a
    transition (regression lock replacing the falsified G3 recorder fix).

    After D2 ``circuit_breaker`` is the Prometheus ``CBMetricRecorder``, which is
    set-on-transition (identical to the former native recorder's observable
    gauge). ``record_state_change`` calls ``set_state(to_state)``, so the state
    gauge surfaces in the exposition.
    """

    def test_record_state_change_exports_state_series(self, otel_metrics):
        """A CB transition makes baldur_circuit_breaker_state appear with the
        target state's numeric value (open=1)."""
        otel_metrics.circuit_breaker.record_state_change("otel_d5_cb", "closed", "open")

        # Labels render sorted: cell_id before service (default cell_id="").
        assert (
            'baldur_circuit_breaker_state{cell_id="",service="otel_d5_cb"} 1.0'
            in _exposition()
        )


# 648 D1: the families that reuse their Prometheus recorder, instantiated in
# OTELBaldurMetrics._init_reused_prometheus_recorders. The native retry/replay/
# infra recorders are wired separately in __init__ and excluded here.
_REUSED_FAMILIES = (
    "dlq",
    "circuit_breaker",
    "throttle",
    "bulkhead",
    "correlation_engine",
    "auto_tuning",
    "recommendation",
    "health_check",
    "shutdown",
    "system_control",
    "emergency_mode",
    "event_bus",
    "hedging",
    "pool_monitor",
    "canary",
    "runtime_config",
    "corruption_shield",
    "learning",
    "forecaster",
    "daily_report",
    "watchdog",
    "notification",
    "postmortem",
    "governance",
    "entitlement",
    "protect",
    "idempotency",
    "executor",
    "daemon_workers",
)


class TestOtelBackendReusedFamilies:
    """648 D1 — the OTel backend wires every reused Prometheus-recorder family
    (zero-arg constructors, idempotent against the shared REGISTRY)."""

    def test_all_reused_families_are_present(self, otel_metrics):
        """Contract: every family named in D1 is a live recorder attribute on the
        initialized OTel backend (was 25 absent before the family gap closed)."""
        missing = [
            family
            for family in _REUSED_FAMILIES
            if getattr(otel_metrics, family, None) is None
        ]
        assert not missing, f"reused families absent on the OTel backend: {missing}"

    def test_reused_family_count_is_twenty_nine(self, otel_metrics):
        """Contract: exactly the 29 reused families are wired (guards against a
        silent add/drop diverging from the design list)."""
        present = [f for f in _REUSED_FAMILIES if hasattr(otel_metrics, f)]
        assert len(present) == 29

    def test_constructing_backend_twice_is_idempotent(self):
        """The zero-arg recorder constructors register against the shared REGISTRY
        via get_or_create_*, so a second backend instance raises no
        duplicate-registration error and is also fully initialized."""
        with patch("baldur.observability.get_meter", return_value=MagicMock()):
            first = OTELBaldurMetrics()
            second = OTELBaldurMetrics()

        assert first._initialized is True
        assert second._initialized is True


# =============================================================================
# 651 — OTel-native histogram bucket-boundary parity (D2/D4/D7)
# =============================================================================


@pytest.fixture
def real_otel_backend(monkeypatch):
    """A REAL meter-backed OTel backend (NOT the MagicMock-meter ``otel_metrics``).

    The bucket-boundary assertions need real OTel histogram instruments whose
    ``PrometheusMetricReader`` bridge surfaces the scraped ``le`` set — a MagicMock
    meter returns mocks with no boundaries and ``record`` produces no scrape
    series (Testability Notes). Mirrors the G46 ``backends`` fixture: pins the OTel
    profile via ``monkeypatch.setenv`` (function-scoped — NOT
    ``patch.dict(clear=True)``, which strips the suite's env pins and leaks
    singletons across xdist workers; see UNIT_TEST_GUIDELINES §6.5), resets the
    observability settings singleton, and builds the meter provider before
    constructing the backend.
    """
    pytest.importorskip("opentelemetry")
    monkeypatch.setenv("BALDUR_OBSERVABILITY_PROFILE", "otel_collector")
    from baldur.settings.observability import reset_observability_settings

    reset_observability_settings()
    from baldur.observability import get_meter, initialize_meter_provider

    initialize_meter_provider()
    assert get_meter() is not None, (
        "OTel meter provider failed to initialize in an otel-equipped env — the "
        "bucket-parity assertions require a real meter-backed backend"
    )

    backend = OTELBaldurMetrics(prefix="baldur")
    assert backend._initialized is True
    try:
        yield backend
    finally:
        reset_observability_settings()


def _bucket_le_floats(metric_name: str) -> set[float]:
    """Finite ``le`` boundary values exposed for ``<metric_name>_bucket``.

    Parses ``le`` as float (the OTel→prometheus bridge renders integer boundaries
    as ``le="3"`` while prometheus_client renders ``le="3.0"`` — float-parsing
    normalizes both). ``+Inf`` is excluded; all label-series of one metric share
    the identical bucket config, so the union over labels is the boundary set.
    """
    out: set[float] = set()
    prefix = metric_name + "_bucket"
    for line in _exposition().splitlines():
        if not line.startswith(prefix):
            continue
        match = re.search(r'le="([^"]+)"', line)
        if match and match.group(1) != "+Inf":
            out.add(float(match.group(1)))
    return out


def _bucket_counts(metric_name: str, label_filter: str) -> dict[float, float]:
    """Map ``le`` (as float; ``+Inf`` → ``inf``) → cumulative count for the one
    ``<metric_name>_bucket`` series whose label set contains ``label_filter``."""
    out: dict[float, float] = {}
    prefix = metric_name + "_bucket"
    for line in _exposition().splitlines():
        if not line.startswith(prefix) or label_filter not in line:
            continue
        match = re.match(r"^\S+\{(.*)\}\s+(\S+)$", line)
        if not match:
            continue
        le = re.search(r'le="([^"]+)"', match.group(1))
        if le:
            out[float(le.group(1))] = float(match.group(2))
    return out


# 651 D1 — each native OTel histogram and the canonical bucket tuple its
# Prometheus recorder defines. The record closure surfaces the metric (an
# OTel histogram with zero observations does not export) with a label unique to
# this test. capacity_warmup_duration has no public recording method, so it is
# observed directly on the instrument.
_NATIVE_HISTOGRAM_BUCKETS = [
    pytest.param(
        lambda m: m.infra.record_http_request("GET", "g47_http", 200, 0.0044),
        "baldur_http_request_duration_seconds",
        (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
        id="http_request_duration_seconds",
    ),
    pytest.param(
        lambda m: m.retry.record_retry("g47_delay", True, 12.0),
        "baldur_retry_delay_seconds",
        (1, 5, 10, 30, 60, 120, 300, 600),
        id="retry_delay_seconds",
    ),
    pytest.param(
        lambda m: m.retry.record_recovery_duration("g47_rec", "auto", 120.0),
        "baldur_recovery_time_seconds",
        (60, 300, 900, 1800, 3600, 7200, 14400, 28800, 86400),
        id="recovery_time_seconds",
    ),
    pytest.param(
        lambda m: m.replay.record_replay("g47_replay", "success", 1.5),
        "baldur_replay_duration_seconds",
        (0.1, 0.5, 1, 2, 5, 10, 30),
        id="replay_duration_seconds",
    ),
    pytest.param(
        lambda m: m.infra._capacity_warmup_duration.record(2.0, {}),
        "baldur_capacity_warmup_duration_seconds",
        (0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0),
        id="capacity_warmup_duration_seconds",
    ),
    pytest.param(
        lambda m: m.retry.record_attempt("g47_att", 3, "success"),
        "baldur_retry_attempts_distribution",
        (1, 2, 3, 4, 5, 6, 7, 8, 9, 10),
        id="retry_attempts_distribution",
    ),
]


class TestNativeHistogramBucketParity:
    """651 D2/D4 — under the OTel backend every native histogram exposes the
    bucket ``le`` set its Prometheus recorder defines (not the OTel SDK's default
    millisecond-scale boundaries).

    Before the advisory was added, ``meter.create_histogram`` with no boundaries
    let the SDK apply ``[0, 5, …, 10000]`` to seconds-valued metrics, so
    ``histogram_quantile`` reported seconds–minutes for millisecond requests. The
    fix passes ``explicit_bucket_boundaries_advisory`` mirroring each Prometheus
    recorder's ``buckets=`` tuple.
    """

    @pytest.mark.parametrize(
        ("record_fn", "metric_name", "expected_buckets"), _NATIVE_HISTOGRAM_BUCKETS
    )
    def test_native_histogram_exposes_prometheus_bucket_le_set(
        self, real_otel_backend, record_fn, metric_name, expected_buckets
    ):
        """The scraped ``le`` set equals the metric's D1 canonical buckets (SC1/SC3)
        — proves the advisory overrode the SDK default (e.g. http carries
        ``0.005…10.0`` and NOT the SDK ``10000``)."""
        record_fn(real_otel_backend)

        scraped = _bucket_le_floats(metric_name)
        expected = {float(b) for b in expected_buckets}
        assert scraped == expected, (
            f"{metric_name} bucket le set diverges from its Prometheus recorder — "
            f"the advisory boundaries were not applied.\n"
            f"scraped: {sorted(scraped)}\nexpected: {sorted(expected)}"
        )
        # The SDK default boundaries (ms-scale) must be gone for the seconds metric.
        if metric_name == "baldur_http_request_duration_seconds":
            assert 10000.0 not in scraped

    def test_subsecond_request_lands_in_lowest_bucket(self, real_otel_backend):
        """A sub-10 ms request lands in a sub-second bucket, so P99 ≤ 0.01 s (SC2).

        Cumulative count at ``le=0.01`` equals the total — the observation did NOT
        collapse into the SDK-default ``0 → 5`` (seconds) band that made the live
        panel render minutes.
        """
        real_otel_backend.infra.record_http_request("GET", "g47_subsec", 200, 0.0044)

        counts = _bucket_counts(
            "baldur_http_request_duration_seconds", 'endpoint="g47_subsec"'
        )
        total = counts[float("inf")]
        assert total >= 1.0
        assert counts[0.01] == total

    def test_attempts_distribution_boundary_landing_bucket(self, real_otel_backend):
        """D7 — an integer observation equal to a boundary lands in that boundary's
        bucket (``le`` / ``bisect_left`` semantics), and an over-range count lands
        only in ``+Inf`` (SC4).

        Guards against a future SDK switching ``bisect_left`` → ``bisect_right``,
        which would silently shift every retry-attempt observation one bucket while
        value-parity (G47) still passed.
        """
        # attempt_count == 3 is included at le=3 (NOT first counted at le=4).
        real_otel_backend.retry.record_attempt("g47_attempt3", 3, "success")
        c3 = _bucket_counts(
            "baldur_retry_attempts_distribution", 'domain="g47_attempt3"'
        )
        assert c3[3.0] == 1.0
        assert c3[2.0] == 0.0

        # An over-range count (11) appears only in +Inf, not the top finite bucket.
        real_otel_backend.retry.record_attempt("g47_attempt11", 11, "success")
        c11 = _bucket_counts(
            "baldur_retry_attempts_distribution", 'domain="g47_attempt11"'
        )
        assert c11[10.0] == 0.0
        assert c11[float("inf")] == 1.0
