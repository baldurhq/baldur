"""Backend-parity tests for the metric event handlers (645).

The event handlers route every domain metric through the recorder *public*
methods (record_state_change / record_trip / record_failure / record_attempt /
record_retry / record_recovery_duration / record_sla_breach / record_started /
record_replay / record_store_duration) so that Circuit Breaker / DLQ / retry /
replay metrics populate under BOTH the prometheus_client backend (BaldurMetrics)
and the OTel backend (OTELBaldurMetrics).

Pre-645 the handlers reached into prometheus_client-private recorder attributes
(metrics.<recorder>._<private>.labels().set/inc/observe), which raise an
AttributeError under the OTel recorder twins. The handlers' fail-open
``try/except`` swallowed that AttributeError (logged once as a ``*_failed``
event), so the metric was silently dropped. This file pins the fix:

* Driving each handler family through a *real* backend records the series with
  no swallowed ``*_failed`` event (the silent-drop regression signal), under
  BOTH backends.
* The 3 recorder method-pairs added for parity (record_recovery_duration,
  record_started, OTel record_store_duration) behave correctly on both backends.
* ``is_synthetic`` on the handler path is now context-derived (was hard-coded
  "false"), production behaviour unchanged.

OTel activation follows the established test_otel_backend.py pattern: patch
``baldur.observability.get_meter`` with a MagicMock meter so the OTel recorder
twins initialize without a live Collector. The real-SDK aggregation +
PrometheusMetricReader bridge layer (which the MagicMock meter stubs) is covered
separately by tests/integration/otel/test_event_handler_prometheus_bridge.py.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest
from structlog.testing import capture_logs

import baldur.metrics.event_handlers as eh
from baldur.core.test_mode_context import TestModeContext
from baldur.metrics.event_handlers import (
    CircuitBreakerEventHandler,
    DLQMetricEventHandler,
    ReplayEventHandler,
    reset_event_handler_cache,
)
from baldur.metrics.otel_backend import OTELBaldurMetrics

# A registered domain resolves through resolve_domain_label() unchanged, so the
# spy assertions can pin the exact pass-through value.
_REGISTERED_DOMAIN = "external_service"


def _failure_events(logs: list[dict]) -> list[dict]:
    """Captured structlog events whose name signals a swallowed failure.

    Both the handler-level (``event_handler.record_*_failed``) and recorder-level
    (``metrics.record_*_failed``) fail-open envelopes use a ``_failed`` suffix, so
    an empty result proves the recording path executed without a swallowed error.
    """
    return [e for e in logs if str(e.get("event", "")).endswith("_failed")]


def _cb_state_value(metrics, service: str, cell_id: str = "") -> float | None:
    """Read the circuit_breaker_state series value under either backend.

    The OTel backend stores observable-gauge values in a real ``_GaugeStore``
    (independent of the mocked meter); the prometheus backend exposes a real
    gauge child. Returns ``None`` if the OTel series is absent.
    """
    cb = metrics.circuit_breaker
    state_store = getattr(cb, "_state_store", None)
    if state_store is not None:  # OTel backend
        key = tuple(sorted({"service": service, "cell_id": cell_id}.items()))
        return state_store._values.get(key)
    # prometheus backend
    return cb._state.labels(service=service, cell_id=cell_id)._value.get()


def _spy_on(obj, method_name: str) -> MagicMock:
    """Wrap a bound recorder method with ``MagicMock(wraps=...)``.

    Calls are recorded while the real implementation still runs — so a passing
    ``assert_called`` proves the handler reached the recorder, and an empty
    ``_failure_events`` proves that real implementation executed without a
    swallowed AttributeError.
    """
    spy = MagicMock(wraps=getattr(obj, method_name))
    setattr(obj, method_name, spy)
    return spy


# =============================================================================
# Cross-backend handler parity (SC1, SC3)
# =============================================================================


@pytest.fixture(params=["prometheus", "otel"])
def backend(request):
    """A real metrics backend injected into the event-handler module.

    ``prometheus`` builds a real BaldurMetrics (prometheus_client recorders);
    ``otel`` builds a real OTELBaldurMetrics whose meter is a MagicMock (the
    established activation pattern), exercising the OTel recorder twins without a
    live Collector. The event-handler caches are reset around the test so the
    injected instance never leaks. Yields ``(name, metrics)``.
    """
    reset_event_handler_cache()
    if request.param == "prometheus":
        from baldur.metrics.prometheus import BaldurMetrics

        metrics = BaldurMetrics()
    else:
        with patch("baldur.observability.get_meter", return_value=MagicMock()):
            metrics = OTELBaldurMetrics()
        assert metrics._initialized, "OTEL backend failed to initialize"
    eh._metrics_instance = metrics
    yield request.param, metrics
    reset_event_handler_cache()


class TestEventHandlerBackendParity:
    """Behavior: every handler family records under BOTH backends with no
    swallowed AttributeError (the 645 silent-drop regression).
    """

    @pytest.mark.parametrize(
        ("to_state", "expected_value"),
        [("closed", 0.0), ("open", 1.0), ("half_open", 2.0)],
    )
    def test_on_state_changed_populates_cb_state_series(
        self, backend, to_state, expected_value
    ):
        """on_state_changed records the dashboard cb-state series, no *_failed."""
        _name, metrics = backend
        service = "parity_cb_state_svc"

        with capture_logs() as logs:
            CircuitBreakerEventHandler.on_state_changed(service, "closed", to_state)

        assert _failure_events(logs) == []
        assert _cb_state_value(metrics, service) == expected_value

    def test_on_state_changed_to_open_records_trip(self, backend):
        """A transition to open routes through record_trip without *_failed."""
        _name, metrics = backend
        spy = _spy_on(metrics.circuit_breaker, "record_trip")

        with capture_logs() as logs:
            CircuitBreakerEventHandler.on_state_changed(
                "parity_trip_svc", "closed", "open"
            )

        assert _failure_events(logs) == []
        spy.assert_called_once_with("parity_trip_svc")

    def test_on_state_changed_non_open_skips_trip(self, backend):
        """A non-open transition records the state change but no trip."""
        _name, metrics = backend
        spy = _spy_on(metrics.circuit_breaker, "record_trip")

        with capture_logs() as logs:
            CircuitBreakerEventHandler.on_state_changed(
                "parity_notrip_svc", "open", "closed"
            )

        assert _failure_events(logs) == []
        spy.assert_not_called()

    def test_on_failure_records_failure(self, backend):
        """on_failure routes through record_failure without *_failed."""
        _name, metrics = backend
        spy = _spy_on(metrics.circuit_breaker, "record_failure")

        with capture_logs() as logs:
            CircuitBreakerEventHandler.on_failure("parity_fail_svc")

        assert _failure_events(logs) == []
        spy.assert_called_once_with("parity_fail_svc")

    def test_on_item_created_with_duration_records_store_duration(self, backend):
        """on_item_created routes the store-duration through the recorder method."""
        _name, metrics = backend
        spy = _spy_on(metrics.dlq, "record_store_duration")

        with capture_logs() as logs:
            DLQMetricEventHandler.on_item_created(
                _REGISTERED_DOMAIN, "PG_TIMEOUT", duration_seconds=0.05
            )

        assert _failure_events(logs) == []
        spy.assert_called_once_with(_REGISTERED_DOMAIN, 0.05)

    def test_on_item_resolved_records_recovery_and_outcome(self, backend):
        """on_item_resolved routes recovery-duration + success outcome cleanly."""
        _name, metrics = backend
        recovery_spy = _spy_on(metrics.retry, "record_recovery_duration")
        outcome_spy = _spy_on(metrics.retry, "record_retry")

        with capture_logs() as logs:
            DLQMetricEventHandler.on_item_resolved(
                _REGISTERED_DOMAIN, "auto_replay", duration_seconds=30.0
            )

        assert _failure_events(logs) == []
        recovery_spy.assert_called_once_with(_REGISTERED_DOMAIN, "auto_replay", 30.0)
        outcome_spy.assert_called_once_with(_REGISTERED_DOMAIN, True)

    def test_on_item_failed_records_failure_attempt(self, backend):
        """on_item_failed routes through record_attempt with the failure outcome."""
        _name, metrics = backend
        spy = _spy_on(metrics.retry, "record_attempt")

        with capture_logs() as logs:
            DLQMetricEventHandler.on_item_failed(
                _REGISTERED_DOMAIN, "PG_TIMEOUT", attempt_count=3
            )

        assert _failure_events(logs) == []
        spy.assert_called_once_with(_REGISTERED_DOMAIN, 3, "failure")

    def test_on_sla_breach_records_breach(self, backend):
        """on_sla_breach routes through record_sla_breach without *_failed."""
        _name, metrics = backend
        spy = _spy_on(metrics.retry, "record_sla_breach")

        with capture_logs() as logs:
            DLQMetricEventHandler.on_sla_breach(_REGISTERED_DOMAIN)

        assert _failure_events(logs) == []
        spy.assert_called_once_with(_REGISTERED_DOMAIN)

    def test_on_replay_started_records_start(self, backend):
        """on_replay_started routes through record_started without *_failed."""
        _name, metrics = backend
        spy = _spy_on(metrics.replay, "record_started")

        with capture_logs() as logs:
            ReplayEventHandler.on_replay_started(_REGISTERED_DOMAIN, "auto")

        assert _failure_events(logs) == []
        spy.assert_called_once_with(_REGISTERED_DOMAIN, "auto")

    @pytest.mark.parametrize(
        ("success", "outcome"), [(True, "success"), (False, "failure")]
    )
    def test_on_replay_completed_records_outcome(self, backend, success, outcome):
        """on_replay_completed routes through record_replay with the outcome."""
        _name, metrics = backend
        spy = _spy_on(metrics.replay, "record_replay")

        with capture_logs() as logs:
            ReplayEventHandler.on_replay_completed(_REGISTERED_DOMAIN, success, 2.5)

        assert _failure_events(logs) == []
        spy.assert_called_once_with(_REGISTERED_DOMAIN, outcome, 2.5)


# =============================================================================
# is_synthetic flip on the handler path (645 D-decision)
# =============================================================================


class TestEventHandlerSyntheticLabel:
    """Behavior: is_synthetic on the handler path is context-derived.

    Pre-645 the handler hard-coded ``is_synthetic="false"``; it now delegates to
    the recorder, which derives the value from ``TestModeContext``. Production
    (no context) → "false"; inside synthetic/test mode → "true". The production
    path is unchanged; only synthetic/chaos traffic now labels correctly. Unique
    service names isolate each assertion from the shared prometheus registry.
    """

    def test_prometheus_transition_label_false_in_production(self):
        """No TestModeContext → the transitions series carries is_synthetic=false."""
        from baldur.metrics.prometheus import BaldurMetrics

        reset_event_handler_cache()
        metrics = BaldurMetrics()
        eh._metrics_instance = metrics
        try:
            CircuitBreakerEventHandler.on_state_changed(
                "synthetic_prod_svc", "closed", "open"
            )

            child = metrics.circuit_breaker._transitions_total.labels(
                service="synthetic_prod_svc",
                cell_id="",
                from_state="closed",
                to_state="open",
                is_synthetic="false",
            )
            assert child._value.get() == 1.0
        finally:
            reset_event_handler_cache()

    def test_prometheus_transition_label_true_in_synthetic_mode(self):
        """Inside TestModeContext → the transitions series carries is_synthetic=true."""
        from baldur.metrics.prometheus import BaldurMetrics

        reset_event_handler_cache()
        metrics = BaldurMetrics()
        eh._metrics_instance = metrics
        try:
            with TestModeContext.start():
                CircuitBreakerEventHandler.on_state_changed(
                    "synthetic_test_svc", "closed", "open"
                )

            child = metrics.circuit_breaker._transitions_total.labels(
                service="synthetic_test_svc",
                cell_id="",
                from_state="closed",
                to_state="open",
                is_synthetic="true",
            )
            assert child._value.get() == 1.0
        finally:
            reset_event_handler_cache()

    def test_otel_transition_label_true_in_synthetic_mode(self):
        """648 D2: the OTel backend's cb is the reused prometheus recorder, so the
        transition path derives is_synthetic=true under test mode identically to
        the prometheus path."""
        reset_event_handler_cache()
        with patch("baldur.observability.get_meter", return_value=MagicMock()):
            metrics = OTELBaldurMetrics()
        eh._metrics_instance = metrics
        try:
            with TestModeContext.start():
                # Non-open transition: no trip, so the transitions counter is the
                # only counter touched — its labels are unambiguous.
                CircuitBreakerEventHandler.on_state_changed(
                    "otel_synth_svc", "closed", "half_open"
                )

            child = metrics.circuit_breaker._transitions_total.labels(
                service="otel_synth_svc",
                cell_id="",
                from_state="closed",
                to_state="half_open",
                is_synthetic="true",
            )
            assert child._value.get() == 1.0
        finally:
            reset_event_handler_cache()


# =============================================================================
# Recorder method-pairs added for parity (645 D5)
# =============================================================================


class TestRetryRecorderRecoveryDurationBehavior:
    """Behavior: RetryMetricRecorder.record_recovery_duration + delegation (D5)."""

    @pytest.fixture
    def retry_recorder(self):
        from baldur.metrics.recorders.retry import RetryMetricRecorder

        return RetryMetricRecorder()

    def test_record_recovery_duration_observes_histogram(self, retry_recorder):
        """record_recovery_duration observes the pre-computed duration."""
        mock_histogram = MagicMock()
        retry_recorder._recovery_time_seconds = mock_histogram

        retry_recorder.record_recovery_duration("payment", "auto_replay", 123.0)

        mock_histogram.labels.assert_called_once_with(
            domain="payment", resolution_type="auto_replay"
        )
        mock_histogram.labels().observe.assert_called_once_with(123.0)

    def test_record_recovery_duration_accepts_zero_seconds(self, retry_recorder):
        """Boundary: a zero-second recovery (resolved == created) still observes."""
        mock_histogram = MagicMock()
        retry_recorder._recovery_time_seconds = mock_histogram

        retry_recorder.record_recovery_duration("payment", "manual", 0.0)

        mock_histogram.labels().observe.assert_called_once_with(0.0)

    def test_record_recovery_time_delegates_to_record_recovery_duration(
        self, retry_recorder
    ):
        """The datetime-based API computes the duration and delegates."""
        created = datetime(2025, 1, 1, 0, 0, 0, tzinfo=UTC)
        resolved = datetime(2025, 1, 1, 0, 5, 0, tzinfo=UTC)

        with patch.object(retry_recorder, "record_recovery_duration") as mock_dur:
            retry_recorder.record_recovery_time(
                "payment", "auto_replay", created, resolved
            )

        # 5 minutes == 300 seconds.
        mock_dur.assert_called_once_with("payment", "auto_replay", 300.0)


class TestReplayRecorderStartedBehavior:
    """Behavior: ReplayMetricRecorder.record_started increments attempts only (D5)."""

    @pytest.fixture
    def replay_recorder(self):
        from baldur.metrics.recorders.replay import ReplayMetricRecorder

        return ReplayMetricRecorder()

    def test_record_started_increments_attempts_counter(self, replay_recorder):
        """record_started increments the attempts counter."""
        mock_attempts = MagicMock()
        mock_outcomes = MagicMock()
        replay_recorder._attempts_total = mock_attempts
        replay_recorder._outcomes_total = mock_outcomes

        with patch.object(
            replay_recorder, "_get_synthetic_label", return_value="false"
        ):
            replay_recorder.record_started("payment", "auto")

        mock_attempts.labels.assert_called_once_with(
            domain="payment", replay_type="auto", is_synthetic="false"
        )
        mock_attempts.labels().inc.assert_called_once()

    def test_record_started_does_not_touch_outcomes_counter(self, replay_recorder):
        """State-transition: the outcome is unknown at start, so only attempts move."""
        mock_attempts = MagicMock()
        mock_outcomes = MagicMock()
        replay_recorder._attempts_total = mock_attempts
        replay_recorder._outcomes_total = mock_outcomes

        with patch.object(
            replay_recorder, "_get_synthetic_label", return_value="false"
        ):
            replay_recorder.record_started("payment", "auto")

        mock_outcomes.labels.assert_not_called()


class TestOTELRecorderParity:
    """Contract: the OTel recorder twins expose the D5 parity methods and emit to
    the underlying OTel instruments (mocked-meter activation, distinct per-name
    instruments so per-instrument assertions are unambiguous).
    """

    @pytest.fixture
    def otel_metrics(self):
        def _factory(name, **_kw):
            return MagicMock(name=name)

        meter = MagicMock()
        meter.create_counter.side_effect = _factory
        meter.create_histogram.side_effect = _factory
        meter.create_observable_gauge.side_effect = _factory

        with patch("baldur.observability.get_meter", return_value=meter):
            metrics = OTELBaldurMetrics()
        assert metrics._initialized
        return metrics

    def test_otel_retry_record_recovery_duration_records_histogram(self, otel_metrics):
        """_OTELRetryRecorder.record_recovery_duration records to the histogram."""
        otel_metrics.retry.record_recovery_duration("payment", "auto_replay", 50.0)

        otel_metrics.retry._recovery_time.record.assert_called_once_with(
            50.0, {"domain": "payment", "resolution_type": "auto_replay"}
        )

    def test_otel_retry_record_recovery_time_delegates_to_duration(self, otel_metrics):
        """The OTel datetime-based API delegates to record_recovery_duration."""
        created = datetime(2025, 1, 1, 0, 0, 0, tzinfo=UTC)
        resolved = datetime(2025, 1, 1, 0, 0, 30, tzinfo=UTC)

        otel_metrics.retry.record_recovery_time(
            "payment", "auto_replay", created, resolved
        )

        otel_metrics.retry._recovery_time.record.assert_called_once_with(
            30.0, {"domain": "payment", "resolution_type": "auto_replay"}
        )

    def test_otel_replay_record_started_increments_attempts_only(self, otel_metrics):
        """_OTELReplayRecorder.record_started adds attempts but not outcomes."""
        otel_metrics.replay.record_started("payment", "auto")

        otel_metrics.replay._attempts_total.add.assert_called_once_with(
            1, {"domain": "payment", "replay_type": "auto", "is_synthetic": "false"}
        )
        otel_metrics.replay._outcomes_total.add.assert_not_called()

    def test_otel_dlq_is_reused_prometheus_recorder(self, otel_metrics):
        """648 D2: the OTel backend's dlq is the prometheus DLQMetricRecorder
        (reuse for full method coverage), not the former incomplete native twin."""
        from baldur.metrics.recorders.dlq import DLQMetricRecorder

        assert isinstance(otel_metrics.dlq, DLQMetricRecorder)
        assert otel_metrics.dlq._store_duration_seconds is not None

    def test_otel_dlq_record_store_duration_surfaces_in_exposition(self, otel_metrics):
        """648 D2: record_store_duration on the reused prometheus recorder surfaces
        in the shared prometheus_client exposition."""
        from prometheus_client import generate_latest

        otel_metrics.dlq.record_store_duration("payment", 0.05)

        output = generate_latest().decode()
        assert "baldur_dlq_store_duration_seconds_count" in output
