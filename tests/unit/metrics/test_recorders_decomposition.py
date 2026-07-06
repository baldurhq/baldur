"""
Metrics Recorder Decomposition Unit Tests (354 God Object Refactoring).

Test targets:
    - baldur.metrics.protocols.MetricsBackend (Protocol contract)
    - baldur.metrics.recorders.base.BaseMetricRecorder (shared utilities)
    - baldur.metrics.recorders.dlq.DLQMetricRecorder (8 methods)
    - baldur.metrics.recorders.circuit_breaker.CBMetricRecorder (5 methods)
    - baldur.metrics.recorders.retry.RetryMetricRecorder (5 methods)
    - baldur.metrics.recorders.replay.ReplayMetricRecorder (2 methods)
    - baldur.metrics.recorders.infrastructure.InfraMetricRecorder

Test Categories:
    A. Contract: Hardcoded structural/value assertions per design decisions
    B. Behavior: Source-referenced functional verification

Reference:
    docs/baldur/middleware_system/354_GOD_OBJECT_REFACTORING.md
    D13: baldur_ prefix standard
    D14: is_synthetic label on relevant counters
    D15: "service" label (not "service_name") for circuit breaker
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def dlq_recorder():
    """Create a DLQMetricRecorder instance."""
    from baldur.metrics.recorders.dlq import DLQMetricRecorder

    return DLQMetricRecorder()


@pytest.fixture
def cb_recorder():
    """Create a CBMetricRecorder instance."""
    from baldur.metrics.recorders.circuit_breaker import CBMetricRecorder

    return CBMetricRecorder()


@pytest.fixture
def retry_recorder():
    """Create a RetryMetricRecorder instance."""
    from baldur.metrics.recorders.retry import RetryMetricRecorder

    return RetryMetricRecorder()


@pytest.fixture
def replay_recorder():
    """Create a ReplayMetricRecorder instance."""
    from baldur.metrics.recorders.replay import ReplayMetricRecorder

    return ReplayMetricRecorder()


@pytest.fixture
def infra_recorder():
    """Create an InfraMetricRecorder instance."""
    from baldur.metrics.recorders.infrastructure import InfraMetricRecorder

    return InfraMetricRecorder()


# =============================================================================
# A-1. MetricsBackend Protocol Contract
# =============================================================================


def _protocol_attrs(proto: type) -> set[str]:
    """Member names declared on a runtime_checkable Protocol.

    ``__protocol_attrs__`` is a CPython class attribute only from 3.12; on
    3.11 the same set is computed by the private ``_get_protocol_attrs``
    helper. Fall back so the contract assertions hold on every supported
    interpreter (3.11/3.12/3.13).
    """
    cached = getattr(proto, "__protocol_attrs__", None)
    if cached is not None:
        return set(cached)
    from typing import _get_protocol_attrs  # type: ignore[attr-defined]

    return set(_get_protocol_attrs(proto))


class TestMetricsBackendProtocolContract:
    """MetricsBackend Protocol has exactly 5 required recorder attributes."""

    def test_protocol_has_dlq_attribute(self):
        """MetricsBackend Protocol defines dlq attribute."""
        from baldur.metrics.protocols import MetricsBackend

        assert "dlq" in _protocol_attrs(MetricsBackend)

    def test_protocol_has_retry_attribute(self):
        """MetricsBackend Protocol defines retry attribute."""
        from baldur.metrics.protocols import MetricsBackend

        assert "retry" in _protocol_attrs(MetricsBackend)

    def test_protocol_has_circuit_breaker_attribute(self):
        """MetricsBackend Protocol defines circuit_breaker attribute."""
        from baldur.metrics.protocols import MetricsBackend

        assert "circuit_breaker" in _protocol_attrs(MetricsBackend)

    def test_protocol_has_replay_attribute(self):
        """MetricsBackend Protocol defines replay attribute."""
        from baldur.metrics.protocols import MetricsBackend

        assert "replay" in _protocol_attrs(MetricsBackend)

    def test_protocol_has_infra_attribute(self):
        """MetricsBackend Protocol defines infra attribute."""
        from baldur.metrics.protocols import MetricsBackend

        assert "infra" in _protocol_attrs(MetricsBackend)

    def test_protocol_declares_six_recorder_attributes(self):
        """MetricsBackend Protocol declares the 6 recorder attributes (D7).

        The Protocol also declares convenience-method delegates (`record_*` /
        `set_*`) that flatten the recorder surface for module-level helpers
        in `metrics/prometheus.py`. Those methods are intentional and not
        counted here — only the recorder attribute set is the contract.
        """
        from baldur.metrics.protocols import MetricsBackend

        expected_recorders = {
            "dlq",
            "retry",
            "circuit_breaker",
            "replay",
            "infra",
            "throttle",
        }
        assert expected_recorders.issubset(_protocol_attrs(MetricsBackend))

    def test_baldurmetrics_is_instance_of_metricsbackend(self):
        """BaldurMetrics satisfies MetricsBackend Protocol."""
        from baldur.metrics.prometheus import BaldurMetrics
        from baldur.metrics.protocols import MetricsBackend

        metrics = BaldurMetrics()
        assert isinstance(metrics, MetricsBackend)

    def test_protocol_is_runtime_checkable(self):
        """MetricsBackend is decorated with @runtime_checkable."""
        from baldur.metrics.prometheus import BaldurMetrics
        from baldur.metrics.protocols import MetricsBackend

        # A @runtime_checkable protocol supports isinstance() without raising
        # TypeError; a plain Protocol would. This is version-robust, unlike
        # probing the 3.12+ __protocol_attrs__ class attribute directly.
        assert isinstance(BaldurMetrics(), MetricsBackend)


# =============================================================================
# A-2. BaseMetricRecorder Contract
# =============================================================================


class TestBaseMetricRecorderContract:
    """BaseMetricRecorder constants and structure."""

    def test_prefix_is_baldur(self):
        """PREFIX class constant is 'baldur' (D13)."""
        from baldur.metrics.recorders.base import BaseMetricRecorder

        assert BaseMetricRecorder.PREFIX == "baldur"

    def test_has_resolve_domain_method(self):
        """BaseMetricRecorder defines _resolve_domain method."""
        from baldur.metrics.recorders.base import BaseMetricRecorder

        assert hasattr(BaseMetricRecorder, "_resolve_domain")
        assert callable(BaseMetricRecorder._resolve_domain)

    def test_has_get_synthetic_label_method(self):
        """BaseMetricRecorder defines _get_synthetic_label method."""
        from baldur.metrics.recorders.base import BaseMetricRecorder

        assert hasattr(BaseMetricRecorder, "_get_synthetic_label")
        assert callable(BaseMetricRecorder._get_synthetic_label)

    def test_has_clamp_non_negative_static(self):
        """BaseMetricRecorder defines _clamp_non_negative as static method."""
        from baldur.metrics.recorders.base import BaseMetricRecorder

        assert hasattr(BaseMetricRecorder, "_clamp_non_negative")

    def test_has_clamp_percentage_static(self):
        """BaseMetricRecorder defines _clamp_percentage as static method."""
        from baldur.metrics.recorders.base import BaseMetricRecorder

        assert hasattr(BaseMetricRecorder, "_clamp_percentage")


# =============================================================================
# B-2. BaseMetricRecorder Behavior
# =============================================================================


class TestBaseMetricRecorderBehavior:
    """BaseMetricRecorder method behaviors."""

    def test_resolve_domain_delegates_to_resolve_domain_label(self):
        """_resolve_domain delegates to registry.resolve_domain_label."""
        from baldur.metrics.recorders.base import BaseMetricRecorder

        recorder = BaseMetricRecorder()
        with patch(
            "baldur.metrics.registry.resolve_domain_label",
            return_value="test_domain",
        ) as mock_resolve:
            result = recorder._resolve_domain("test_domain")

        mock_resolve.assert_called_once_with("test_domain")
        assert result == "test_domain"

    def test_get_synthetic_label_delegates_to_testmodecontext(self):
        """_get_synthetic_label delegates to TestModeContext.get_synthetic_label_value."""
        from baldur.metrics.recorders.base import BaseMetricRecorder

        recorder = BaseMetricRecorder()
        with patch(
            "baldur.core.test_mode_context.TestModeContext.get_synthetic_label_value",
            return_value="false",
        ) as mock_label:
            result = recorder._get_synthetic_label()

        mock_label.assert_called_once()
        assert result == "false"

    def test_clamp_non_negative_clamps_negative_to_zero(self):
        """_clamp_non_negative returns 0 for negative values."""
        from baldur.metrics.recorders.base import BaseMetricRecorder

        result = BaseMetricRecorder._clamp_non_negative(-5.0, "test_metric")
        assert result == 0.0

    def test_clamp_non_negative_passes_positive_through(self):
        """_clamp_non_negative passes positive values unchanged."""
        from baldur.metrics.recorders.base import BaseMetricRecorder

        result = BaseMetricRecorder._clamp_non_negative(42.0, "test_metric")
        assert result == 42.0

    def test_clamp_non_negative_passes_zero_through(self):
        """_clamp_non_negative passes zero unchanged."""
        from baldur.metrics.recorders.base import BaseMetricRecorder

        result = BaseMetricRecorder._clamp_non_negative(0.0, "test_metric")
        assert result == 0.0

    def test_clamp_percentage_clamps_above_100(self):
        """_clamp_percentage clamps values above 100 to 100."""
        from baldur.metrics.recorders.base import BaseMetricRecorder

        result = BaseMetricRecorder._clamp_percentage(150.0, "test_metric")
        assert result == 100.0

    def test_clamp_percentage_clamps_negative_to_zero(self):
        """_clamp_percentage clamps negative values to 0."""
        from baldur.metrics.recorders.base import BaseMetricRecorder

        result = BaseMetricRecorder._clamp_percentage(-10.0, "test_metric")
        assert result == 0.0

    def test_clamp_percentage_passes_valid_through(self):
        """_clamp_percentage passes values in 0-100 range unchanged."""
        from baldur.metrics.recorders.base import BaseMetricRecorder

        result = BaseMetricRecorder._clamp_percentage(75.0, "test_metric")
        assert result == 75.0


# =============================================================================
# A-3. DLQMetricRecorder Contract
# =============================================================================


class TestDLQMetricRecorderContract:
    """DLQ metric attributes have correct names and labels."""

    def test_items_total_has_baldur_prefix(self, dlq_recorder):
        """_items_total metric name starts with baldur_ (D13)."""
        # prometheus_client Counter appends _total internally, base name uses prefix
        assert dlq_recorder._items_total._name.startswith("baldur_dlq_items")

    def test_items_total_has_is_synthetic_label(self, dlq_recorder):
        """_items_total includes is_synthetic label (D14)."""
        assert "is_synthetic" in dlq_recorder._items_total._labelnames

    def test_items_total_has_domain_label(self, dlq_recorder):
        """_items_total includes domain label."""
        assert "domain" in dlq_recorder._items_total._labelnames

    def test_items_total_has_failure_type_label(self, dlq_recorder):
        """_items_total includes failure_type label."""
        assert "failure_type" in dlq_recorder._items_total._labelnames

    def test_pending_gauge_exists(self, dlq_recorder):
        """_pending_gauge attribute exists."""
        assert hasattr(dlq_recorder, "_pending_gauge")

    def test_by_status_gauge_exists(self, dlq_recorder):
        """_by_status_gauge attribute exists."""
        assert hasattr(dlq_recorder, "_by_status_gauge")

    def test_created_total_exists(self, dlq_recorder):
        """_created_total counter exists."""
        assert hasattr(dlq_recorder, "_created_total")

    def test_overflow_total_exists(self, dlq_recorder):
        """_overflow_total counter exists."""
        assert hasattr(dlq_recorder, "_overflow_total")

    def test_evicted_total_exists(self, dlq_recorder):
        """_evicted_total counter exists."""
        assert hasattr(dlq_recorder, "_evicted_total")

    def test_rejected_total_exists(self, dlq_recorder):
        """_rejected_total counter exists."""
        assert hasattr(dlq_recorder, "_rejected_total")

    def test_emergency_purge_total_exists(self, dlq_recorder):
        """_emergency_purge_total counter exists."""
        assert hasattr(dlq_recorder, "_emergency_purge_total")

    def test_size_ratio_exists(self, dlq_recorder):
        """_size_ratio gauge exists."""
        assert hasattr(dlq_recorder, "_size_ratio")

    def test_all_nine_metric_attributes_exist(self, dlq_recorder):
        """DLQ recorder has exactly 9 metric attributes."""
        expected_attrs = {
            "_items_total",
            "_pending_gauge",
            "_by_status_gauge",
            "_created_total",
            "_overflow_total",
            "_evicted_total",
            "_rejected_total",
            "_emergency_purge_total",
            "_size_ratio",
        }
        for attr in expected_attrs:
            assert hasattr(dlq_recorder, attr), f"Missing attribute: {attr}"


# =============================================================================
# B-3. DLQMetricRecorder Behavior
# =============================================================================


class TestDLQMetricRecorderBehavior:
    """DLQ recorder method behaviors."""

    def test_record_item_created_increments_counter(self, dlq_recorder):
        """record_item_created increments _items_total and _created_total."""
        mock_items = MagicMock()
        mock_created = MagicMock()
        dlq_recorder._items_total = mock_items
        dlq_recorder._created_total = mock_created

        with patch.object(dlq_recorder, "_get_synthetic_label", return_value="false"):
            dlq_recorder.record_item_created("payment", "PG_TIMEOUT")

        mock_items.labels.assert_called_once_with(
            domain="payment",
            failure_type="PG_TIMEOUT",
            is_synthetic="false",
        )
        mock_items.labels().inc.assert_called_once()
        mock_created.labels.assert_called_once_with(domain="payment")
        mock_created.labels().inc.assert_called_once()

    def test_set_pending_count_clamps_negative(self, dlq_recorder):
        """set_pending_count clamps negative values to 0."""
        mock_gauge = MagicMock()
        dlq_recorder._pending_gauge = mock_gauge

        dlq_recorder.set_pending_count("payment", -5)

        mock_gauge.labels.assert_called_once_with(domain="payment")
        mock_gauge.labels().set.assert_called_once_with(0.0)

    def test_set_pending_count_passes_positive(self, dlq_recorder):
        """set_pending_count passes positive values through."""
        mock_gauge = MagicMock()
        dlq_recorder._pending_gauge = mock_gauge

        dlq_recorder.set_pending_count("payment", 42)

        mock_gauge.labels().set.assert_called_once_with(42.0)

    def test_set_status_count_sets_gauge(self, dlq_recorder):
        """set_status_count sets _by_status_gauge with clamped value."""
        mock_gauge = MagicMock()
        dlq_recorder._by_status_gauge = mock_gauge

        dlq_recorder.set_status_count("pending", 10)

        mock_gauge.labels.assert_called_once_with(status="pending")
        mock_gauge.labels().set.assert_called_once_with(10.0)

    def test_set_size_ratio_clamps_above_one(self, dlq_recorder):
        """set_size_ratio clamps ratio > 1.0 down to 1.0."""
        mock_gauge = MagicMock()
        dlq_recorder._size_ratio = mock_gauge

        # ratio=1.5 => 1.5*100=150 => clamp_percentage => 100 => /100 => 1.0
        dlq_recorder.set_size_ratio("payment", 1.5)

        mock_gauge.labels.assert_called_once_with(domain="payment")
        mock_gauge.labels().set.assert_called_once_with(1.0)

    def test_set_size_ratio_clamps_negative(self, dlq_recorder):
        """set_size_ratio clamps negative ratio to 0.0."""
        mock_gauge = MagicMock()
        dlq_recorder._size_ratio = mock_gauge

        # ratio=-0.5 => -0.5*100=-50 => clamp_percentage => 0 => /100 => 0.0
        dlq_recorder.set_size_ratio("payment", -0.5)

        mock_gauge.labels().set.assert_called_once_with(0.0)

    def test_set_size_ratio_passes_valid(self, dlq_recorder):
        """set_size_ratio passes valid ratio 0-1 unchanged."""
        mock_gauge = MagicMock()
        dlq_recorder._size_ratio = mock_gauge

        # ratio=0.75 => 0.75*100=75 => clamp_percentage => 75 => /100 => 0.75
        dlq_recorder.set_size_ratio("payment", 0.75)

        mock_gauge.labels().set.assert_called_once_with(0.75)

    def test_record_overflow_increments_counter(self, dlq_recorder):
        """record_overflow increments _overflow_total."""
        mock_counter = MagicMock()
        dlq_recorder._overflow_total = mock_counter

        dlq_recorder.record_overflow("payment", "drop_oldest")

        mock_counter.labels.assert_called_once_with(
            domain="payment", strategy="drop_oldest"
        )
        mock_counter.labels().inc.assert_called_once()

    def test_record_evicted_increments_by_count(self, dlq_recorder):
        """record_evicted increments _evicted_total by count."""
        mock_counter = MagicMock()
        dlq_recorder._evicted_total = mock_counter

        dlq_recorder.record_evicted(5, "drop_oldest", domain="payment")

        mock_counter.labels.assert_called_once_with(
            domain="payment", strategy="drop_oldest"
        )
        mock_counter.labels().inc.assert_called_once_with(5)

    def test_record_rejected_increments_counter(self, dlq_recorder):
        """record_rejected increments _rejected_total."""
        mock_counter = MagicMock()
        dlq_recorder._rejected_total = mock_counter

        dlq_recorder.record_rejected("payment")

        mock_counter.labels.assert_called_once_with(domain="payment")
        mock_counter.labels().inc.assert_called_once()

    def test_record_emergency_purge_increments_counter(self, dlq_recorder):
        """record_emergency_purge increments _emergency_purge_total."""
        mock_counter = MagicMock()
        dlq_recorder._emergency_purge_total = mock_counter

        dlq_recorder.record_emergency_purge()

        mock_counter.inc.assert_called_once()

    def test_record_item_created_no_raise_on_internal_error(self, dlq_recorder):
        """record_item_created does not raise on internal errors (try/except)."""
        mock_counter = MagicMock()
        mock_counter.labels.side_effect = RuntimeError("metric error")
        dlq_recorder._items_total = mock_counter

        # Should not raise
        dlq_recorder.record_item_created("payment", "PG_TIMEOUT")

    def test_set_pending_count_no_raise_on_internal_error(self, dlq_recorder):
        """set_pending_count does not raise on internal errors (try/except)."""
        mock_gauge = MagicMock()
        mock_gauge.labels.side_effect = RuntimeError("metric error")
        dlq_recorder._pending_gauge = mock_gauge

        # Should not raise
        dlq_recorder.set_pending_count("payment", 10)

    def test_record_overflow_no_raise_on_internal_error(self, dlq_recorder):
        """record_overflow does not raise on internal errors (try/except)."""
        mock_counter = MagicMock()
        mock_counter.labels.side_effect = RuntimeError("metric error")
        dlq_recorder._overflow_total = mock_counter

        # Should not raise
        dlq_recorder.record_overflow("payment", "drop_oldest")

    def test_set_size_ratio_no_raise_on_internal_error(self, dlq_recorder):
        """set_size_ratio does not raise on internal errors (try/except)."""
        mock_gauge = MagicMock()
        mock_gauge.labels.side_effect = RuntimeError("metric error")
        dlq_recorder._size_ratio = mock_gauge

        # Should not raise
        dlq_recorder.set_size_ratio("payment", 0.5)


# =============================================================================
# A-4. CBMetricRecorder Contract
# =============================================================================


class TestCBMetricRecorderContract:
    """Circuit Breaker metric attributes and labels."""

    def test_state_gauge_uses_service_label_not_service_name(self, cb_recorder):
        """_state gauge uses 'service' label, not 'service_name' (D15)."""
        assert "service" in cb_recorder._state._labelnames
        assert "service_name" not in cb_recorder._state._labelnames

    def test_failures_total_uses_service_label(self, cb_recorder):
        """_failures_total uses 'service' label (D15)."""
        assert "service" in cb_recorder._failures_total._labelnames

    def test_trips_total_uses_service_label(self, cb_recorder):
        """_trips_total uses 'service' label (D15)."""
        assert "service" in cb_recorder._trips_total._labelnames

    def test_transitions_total_has_is_synthetic_label(self, cb_recorder):
        """_transitions_total includes is_synthetic label (D14)."""
        assert "is_synthetic" in cb_recorder._transitions_total._labelnames

    def test_transitions_total_has_from_to_state_labels(self, cb_recorder):
        """_transitions_total has from_state and to_state labels."""
        assert "from_state" in cb_recorder._transitions_total._labelnames
        assert "to_state" in cb_recorder._transitions_total._labelnames

    def test_state_gauge_has_cell_id_label(self, cb_recorder):
        """_state gauge includes cell_id label."""
        assert "cell_id" in cb_recorder._state._labelnames

    def test_open_duration_histogram_exists(self, cb_recorder):
        """_open_duration histogram exists."""
        assert hasattr(cb_recorder, "_open_duration")

    def test_all_five_metric_attributes_exist(self, cb_recorder):
        """CB recorder has exactly 5 metric attributes."""
        expected_attrs = {
            "_state",
            "_failures_total",
            "_trips_total",
            "_transitions_total",
            "_open_duration",
        }
        for attr in expected_attrs:
            assert hasattr(cb_recorder, attr), f"Missing attribute: {attr}"


# =============================================================================
# B-4. CBMetricRecorder Behavior
# =============================================================================


class TestCBMetricRecorderBehavior:
    """Circuit Breaker recorder method behaviors."""

    def test_set_state_maps_closed_to_zero(self, cb_recorder):
        """set_state maps 'closed' to numeric value 0."""
        mock_gauge = MagicMock()
        cb_recorder._state = mock_gauge

        cb_recorder.set_state("payment-api", "closed")

        mock_gauge.labels.assert_called_once_with(service="payment-api", cell_id="")
        mock_gauge.labels().set.assert_called_once_with(0)

    def test_set_state_maps_open_to_one(self, cb_recorder):
        """set_state maps 'open' to numeric value 1."""
        mock_gauge = MagicMock()
        cb_recorder._state = mock_gauge

        cb_recorder.set_state("payment-api", "open")

        mock_gauge.labels().set.assert_called_once_with(1)

    def test_set_state_maps_half_open_to_two(self, cb_recorder):
        """set_state maps 'half_open' to numeric value 2."""
        mock_gauge = MagicMock()
        cb_recorder._state = mock_gauge

        cb_recorder.set_state("payment-api", "half_open")

        mock_gauge.labels().set.assert_called_once_with(2)

    def test_set_state_maps_unknown_to_zero(self, cb_recorder):
        """set_state maps unknown state string to 0 (default)."""
        mock_gauge = MagicMock()
        cb_recorder._state = mock_gauge

        cb_recorder.set_state("payment-api", "unknown_state")

        mock_gauge.labels().set.assert_called_once_with(0)

    def test_set_state_with_cell_id(self, cb_recorder):
        """set_state passes cell_id label correctly."""
        mock_gauge = MagicMock()
        cb_recorder._state = mock_gauge

        cb_recorder.set_state("payment-api", "open", cell_id="us-east-1")

        mock_gauge.labels.assert_called_once_with(
            service="payment-api", cell_id="us-east-1"
        )

    def test_record_failure_increments_counter(self, cb_recorder):
        """record_failure increments _failures_total."""
        mock_counter = MagicMock()
        cb_recorder._failures_total = mock_counter

        cb_recorder.record_failure("payment-api")

        mock_counter.labels.assert_called_once_with(service="payment-api")
        mock_counter.labels().inc.assert_called_once()

    def test_record_trip_increments_counter(self, cb_recorder):
        """record_trip increments _trips_total."""
        mock_counter = MagicMock()
        cb_recorder._trips_total = mock_counter

        cb_recorder.record_trip("payment-api")

        mock_counter.labels.assert_called_once_with(service="payment-api")
        mock_counter.labels().inc.assert_called_once()

    def test_record_state_change_calls_set_state_and_increments_transitions(
        self, cb_recorder
    ):
        """record_state_change sets state and increments _transitions_total."""
        mock_state = MagicMock()
        mock_transitions = MagicMock()
        cb_recorder._state = mock_state
        cb_recorder._transitions_total = mock_transitions

        with patch.object(cb_recorder, "_get_synthetic_label", return_value="false"):
            cb_recorder.record_state_change(
                "payment-api", "closed", "open", cell_id="us-east-1"
            )

        # Verify set_state was called via _state.labels
        mock_state.labels.assert_called_once_with(
            service="payment-api", cell_id="us-east-1"
        )
        mock_state.labels().set.assert_called_once_with(1)  # "open" => 1

        # Verify transitions counter incremented
        mock_transitions.labels.assert_called_once_with(
            service="payment-api",
            cell_id="us-east-1",
            from_state="closed",
            to_state="open",
            is_synthetic="false",
        )
        mock_transitions.labels().inc.assert_called_once()

    def test_record_open_duration_observes_histogram(self, cb_recorder):
        """record_open_duration observes _open_duration histogram."""
        mock_histogram = MagicMock()
        cb_recorder._open_duration = mock_histogram

        cb_recorder.record_open_duration("payment-api", 120.5)

        mock_histogram.labels.assert_called_once_with(service="payment-api")
        mock_histogram.labels().observe.assert_called_once_with(120.5)

    def test_record_failure_no_raise_on_internal_error(self, cb_recorder):
        """record_failure does not raise on internal errors."""
        mock_counter = MagicMock()
        mock_counter.labels.side_effect = RuntimeError("metric error")
        cb_recorder._failures_total = mock_counter

        # Should not raise
        cb_recorder.record_failure("payment-api")

    def test_record_state_change_no_raise_on_internal_error(self, cb_recorder):
        """record_state_change does not raise on internal errors."""
        mock_state = MagicMock()
        mock_state.labels.side_effect = RuntimeError("metric error")
        cb_recorder._state = mock_state

        # Should not raise
        cb_recorder.record_state_change("payment-api", "closed", "open")


# =============================================================================
# A-5. RetryMetricRecorder Contract
# =============================================================================


class TestRetryMetricRecorderContract:
    """Retry metric attributes and labels."""

    def test_attempts_histogram_has_is_synthetic_label(self, retry_recorder):
        """_attempts_histogram includes is_synthetic label (D14)."""
        assert "is_synthetic" in retry_recorder._attempts_histogram._labelnames

    def test_outcomes_total_has_is_synthetic_label(self, retry_recorder):
        """_outcomes_total includes is_synthetic label (D14)."""
        assert "is_synthetic" in retry_recorder._outcomes_total._labelnames

    def test_success_rate_gauge_exists(self, retry_recorder):
        """_success_rate gauge exists."""
        assert hasattr(retry_recorder, "_success_rate")

    def test_delay_seconds_histogram_exists(self, retry_recorder):
        """_delay_seconds histogram exists."""
        assert hasattr(retry_recorder, "_delay_seconds")

    def test_recovery_time_histogram_exists(self, retry_recorder):
        """_recovery_time_seconds histogram exists."""
        assert hasattr(retry_recorder, "_recovery_time_seconds")

    def test_sla_breach_counter_exists(self, retry_recorder):
        """_sla_breach_total counter exists."""
        assert hasattr(retry_recorder, "_sla_breach_total")


# =============================================================================
# B-5. RetryMetricRecorder Behavior
# =============================================================================


class TestRetryMetricRecorderBehavior:
    """Retry recorder method behaviors."""

    def test_record_attempt_observes_histogram_and_increments_counter(
        self, retry_recorder
    ):
        """record_attempt records histogram and increments outcomes counter."""
        mock_histogram = MagicMock()
        mock_counter = MagicMock()
        retry_recorder._attempts_histogram = mock_histogram
        retry_recorder._outcomes_total = mock_counter

        with patch.object(retry_recorder, "_get_synthetic_label", return_value="false"):
            retry_recorder.record_attempt("payment", 3, "success")

        mock_histogram.labels.assert_called_once_with(
            domain="payment", is_synthetic="false"
        )
        mock_histogram.labels().observe.assert_called_once_with(3)
        mock_counter.labels.assert_called_once_with(
            domain="payment", outcome="success", is_synthetic="false"
        )
        mock_counter.labels().inc.assert_called_once()

    def test_record_retry_with_delay_records_delay_histogram(self, retry_recorder):
        """record_retry with delay records _delay_seconds histogram."""
        mock_outcomes = MagicMock()
        mock_delay = MagicMock()
        retry_recorder._outcomes_total = mock_outcomes
        retry_recorder._delay_seconds = mock_delay

        with patch.object(retry_recorder, "_get_synthetic_label", return_value="false"):
            retry_recorder.record_retry("payment", success=True, delay=5.0)

        mock_delay.labels.assert_called_once_with(domain="payment")
        mock_delay.labels().observe.assert_called_once_with(5.0)

    def test_record_retry_without_delay_skips_delay_histogram(self, retry_recorder):
        """record_retry without delay does not record _delay_seconds."""
        mock_outcomes = MagicMock()
        mock_delay = MagicMock()
        retry_recorder._outcomes_total = mock_outcomes
        retry_recorder._delay_seconds = mock_delay

        with patch.object(retry_recorder, "_get_synthetic_label", return_value="false"):
            retry_recorder.record_retry("payment", success=False)

        mock_delay.labels.assert_not_called()

    def test_record_retry_success_true_maps_to_success_outcome(self, retry_recorder):
        """record_retry with success=True uses 'success' outcome."""
        mock_outcomes = MagicMock()
        retry_recorder._outcomes_total = mock_outcomes

        with patch.object(retry_recorder, "_get_synthetic_label", return_value="false"):
            retry_recorder.record_retry("payment", success=True)

        mock_outcomes.labels.assert_called_once_with(
            domain="payment", outcome="success", is_synthetic="false"
        )

    def test_record_retry_success_false_maps_to_failure_outcome(self, retry_recorder):
        """record_retry with success=False uses 'failure' outcome."""
        mock_outcomes = MagicMock()
        retry_recorder._outcomes_total = mock_outcomes

        with patch.object(retry_recorder, "_get_synthetic_label", return_value="false"):
            retry_recorder.record_retry("payment", success=False)

        mock_outcomes.labels.assert_called_once_with(
            domain="payment", outcome="failure", is_synthetic="false"
        )

    def test_set_success_rate_clamps_above_100(self, retry_recorder):
        """set_success_rate clamps values above 100 to 100."""
        mock_gauge = MagicMock()
        retry_recorder._success_rate = mock_gauge

        retry_recorder.set_success_rate("payment", 150.0)

        mock_gauge.labels.assert_called_once_with(domain="payment")
        mock_gauge.labels().set.assert_called_once_with(100.0)

    def test_set_success_rate_clamps_negative(self, retry_recorder):
        """set_success_rate clamps negative values to 0."""
        mock_gauge = MagicMock()
        retry_recorder._success_rate = mock_gauge

        retry_recorder.set_success_rate("payment", -10.0)

        mock_gauge.labels().set.assert_called_once_with(0.0)

    def test_set_success_rate_passes_valid(self, retry_recorder):
        """set_success_rate passes valid percentage unchanged."""
        mock_gauge = MagicMock()
        retry_recorder._success_rate = mock_gauge

        retry_recorder.set_success_rate("payment", 85.5)

        mock_gauge.labels().set.assert_called_once_with(85.5)

    def test_record_recovery_time_observes_duration(self, retry_recorder):
        """record_recovery_time calculates duration and observes histogram."""
        mock_histogram = MagicMock()
        retry_recorder._recovery_time_seconds = mock_histogram

        created = datetime(2025, 1, 1, 0, 0, 0, tzinfo=UTC)
        resolved = datetime(2025, 1, 1, 1, 0, 0, tzinfo=UTC)

        retry_recorder.record_recovery_time("payment", "auto_replay", created, resolved)

        mock_histogram.labels.assert_called_once_with(
            domain="payment", resolution_type="auto_replay"
        )
        # 1 hour = 3600 seconds
        mock_histogram.labels().observe.assert_called_once_with(3600.0)

    def test_record_sla_breach_increments_counter(self, retry_recorder):
        """record_sla_breach increments _sla_breach_total."""
        mock_counter = MagicMock()
        retry_recorder._sla_breach_total = mock_counter

        retry_recorder.record_sla_breach("payment")

        mock_counter.labels.assert_called_once_with(domain="payment")
        mock_counter.labels().inc.assert_called_once()

    def test_record_attempt_no_raise_on_internal_error(self, retry_recorder):
        """record_attempt does not raise on internal errors."""
        mock_histogram = MagicMock()
        mock_histogram.labels.side_effect = RuntimeError("metric error")
        retry_recorder._attempts_histogram = mock_histogram

        # Should not raise
        retry_recorder.record_attempt("payment", 3, "success")


# =============================================================================
# A-6. ReplayMetricRecorder Contract
# =============================================================================


class TestReplayMetricRecorderContract:
    """Replay metric attributes and labels."""

    def test_attempts_total_has_is_synthetic_label(self, replay_recorder):
        """_attempts_total includes is_synthetic label (D14)."""
        assert "is_synthetic" in replay_recorder._attempts_total._labelnames

    def test_outcomes_total_has_is_synthetic_label(self, replay_recorder):
        """_outcomes_total includes is_synthetic label (D14)."""
        assert "is_synthetic" in replay_recorder._outcomes_total._labelnames

    def test_duration_histogram_exists(self, replay_recorder):
        """_duration_seconds histogram exists."""
        assert hasattr(replay_recorder, "_duration_seconds")

    def test_all_three_metric_attributes_exist(self, replay_recorder):
        """Replay recorder has exactly 3 metric attributes."""
        expected_attrs = {
            "_attempts_total",
            "_outcomes_total",
            "_duration_seconds",
        }
        for attr in expected_attrs:
            assert hasattr(replay_recorder, attr), f"Missing attribute: {attr}"


# =============================================================================
# B-6. ReplayMetricRecorder Behavior
# =============================================================================


class TestReplayMetricRecorderBehavior:
    """Replay recorder method behaviors."""

    def test_record_attempt_increments_attempts_and_outcomes(self, replay_recorder):
        """record_attempt records both _attempts_total and _outcomes_total."""
        mock_attempts = MagicMock()
        mock_outcomes = MagicMock()
        replay_recorder._attempts_total = mock_attempts
        replay_recorder._outcomes_total = mock_outcomes

        with patch.object(
            replay_recorder, "_get_synthetic_label", return_value="false"
        ):
            replay_recorder.record_attempt("payment", "single", True)

        mock_attempts.labels.assert_called_once_with(
            domain="payment",
            replay_type="single",
            is_synthetic="false",
        )
        mock_attempts.labels().inc.assert_called_once()
        mock_outcomes.labels.assert_called_once_with(
            domain="payment",
            outcome="success",
            is_synthetic="false",
        )
        mock_outcomes.labels().inc.assert_called_once()

    def test_record_attempt_failure_maps_to_failure_outcome(self, replay_recorder):
        """record_attempt with success=False uses 'failure' outcome."""
        mock_attempts = MagicMock()
        mock_outcomes = MagicMock()
        replay_recorder._attempts_total = mock_attempts
        replay_recorder._outcomes_total = mock_outcomes

        with patch.object(
            replay_recorder, "_get_synthetic_label", return_value="false"
        ):
            replay_recorder.record_attempt("payment", "batch", False)

        mock_outcomes.labels.assert_called_once_with(
            domain="payment",
            outcome="failure",
            is_synthetic="false",
        )

    def test_record_replay_with_duration_observes_histogram(self, replay_recorder):
        """record_replay with duration records _duration_seconds histogram."""
        mock_outcomes = MagicMock()
        mock_duration = MagicMock()
        replay_recorder._outcomes_total = mock_outcomes
        replay_recorder._duration_seconds = mock_duration

        with patch.object(
            replay_recorder, "_get_synthetic_label", return_value="false"
        ):
            replay_recorder.record_replay("payment", "success", duration=2.5)

        mock_duration.labels.assert_called_once_with(domain="payment")
        mock_duration.labels().observe.assert_called_once_with(2.5)

    def test_record_replay_without_duration_skips_histogram(self, replay_recorder):
        """record_replay without duration does not record histogram."""
        mock_outcomes = MagicMock()
        mock_duration = MagicMock()
        replay_recorder._outcomes_total = mock_outcomes
        replay_recorder._duration_seconds = mock_duration

        with patch.object(
            replay_recorder, "_get_synthetic_label", return_value="false"
        ):
            replay_recorder.record_replay("payment", "failure")

        mock_duration.labels.assert_not_called()

    def test_record_attempt_no_raise_on_internal_error(self, replay_recorder):
        """record_attempt does not raise on internal errors."""
        mock_attempts = MagicMock()
        mock_attempts.labels.side_effect = RuntimeError("metric error")
        replay_recorder._attempts_total = mock_attempts

        # Should not raise
        replay_recorder.record_attempt("payment", "single", True)


# =============================================================================
# A-7. InfraMetricRecorder Contract
# =============================================================================


class TestInfraMetricRecorderContract:
    """Infrastructure metric attributes."""

    def test_http_requests_total_exists(self, infra_recorder):
        """_http_requests_total counter exists."""
        assert hasattr(infra_recorder, "_http_requests_total")

    def test_http_request_duration_exists(self, infra_recorder):
        """_http_request_duration histogram exists."""
        assert hasattr(infra_recorder, "_http_request_duration")

    def test_http_errors_total_exists(self, infra_recorder):
        """_http_errors_total counter exists."""
        assert hasattr(infra_recorder, "_http_errors_total")

    def test_worker_utilization_exists(self, infra_recorder):
        """_worker_utilization gauge exists."""
        assert hasattr(infra_recorder, "_worker_utilization")

    def test_security_incidents_exists(self, infra_recorder):
        """_security_incidents counter exists."""
        assert hasattr(infra_recorder, "_security_incidents")


# =============================================================================
# B-7. InfraMetricRecorder Behavior
# =============================================================================


class TestInfraMetricRecorderBehavior:
    """Infrastructure recorder method behaviors."""

    def test_record_http_request_increments_counter_and_observes_histogram(
        self, infra_recorder
    ):
        """record_http_request increments counter and observes duration."""
        mock_counter = MagicMock()
        mock_histogram = MagicMock()
        infra_recorder._http_requests_total = mock_counter
        infra_recorder._http_request_duration = mock_histogram

        infra_recorder.record_http_request("GET", "/api/v1/health", 200, 0.05)

        mock_counter.labels.assert_called_once_with(
            method="GET",
            endpoint="/api/v1/health",
            status_code="200",
        )
        mock_counter.labels().inc.assert_called_once()
        mock_histogram.labels.assert_called_once_with(
            method="GET", endpoint="/api/v1/health"
        )
        mock_histogram.labels().observe.assert_called_once_with(0.05)

    def test_record_http_request_converts_status_code_to_string(self, infra_recorder):
        """record_http_request converts status_code int to string for label."""
        mock_counter = MagicMock()
        mock_histogram = MagicMock()
        infra_recorder._http_requests_total = mock_counter
        infra_recorder._http_request_duration = mock_histogram

        infra_recorder.record_http_request("POST", "/api/v1/items", 201, 0.1)

        mock_counter.labels.assert_called_once_with(
            method="POST",
            endpoint="/api/v1/items",
            status_code="201",
        )

    def test_set_worker_utilization_clamps_above_one(self, infra_recorder):
        """set_worker_utilization clamps ratio > 1.0 to 1.0."""
        mock_gauge = MagicMock()
        infra_recorder._worker_utilization = mock_gauge

        infra_recorder.set_worker_utilization("main_pool", 1.5)

        mock_gauge.labels.assert_called_once_with(pool_name="main_pool")
        mock_gauge.labels().set.assert_called_once_with(1.0)

    def test_set_worker_utilization_clamps_negative_to_zero(self, infra_recorder):
        """set_worker_utilization clamps negative ratio to 0.0."""
        mock_gauge = MagicMock()
        infra_recorder._worker_utilization = mock_gauge

        infra_recorder.set_worker_utilization("main_pool", -0.5)

        mock_gauge.labels().set.assert_called_once_with(0.0)

    def test_set_worker_utilization_passes_valid(self, infra_recorder):
        """set_worker_utilization passes valid ratio 0-1 unchanged."""
        mock_gauge = MagicMock()
        infra_recorder._worker_utilization = mock_gauge

        infra_recorder.set_worker_utilization("main_pool", 0.75)

        mock_gauge.labels().set.assert_called_once_with(0.75)

    def test_http_request_timer_records_duration(self, infra_recorder):
        """http_request_timer context manager records duration on exit."""
        mock_histogram = MagicMock()
        infra_recorder._http_request_duration = mock_histogram

        with infra_recorder.http_request_timer("GET", "/api/v1/health"):
            pass  # Simulate request

        mock_histogram.labels.assert_called_with(
            method="GET", endpoint="/api/v1/health"
        )
        mock_histogram.labels().observe.assert_called_once()
        # Verify duration is a positive float
        observed_duration = mock_histogram.labels().observe.call_args[0][0]
        assert observed_duration >= 0

    def test_http_request_timer_records_error_on_exception(self, infra_recorder):
        """http_request_timer records error when exception occurs."""
        mock_histogram = MagicMock()
        mock_errors = MagicMock()
        infra_recorder._http_request_duration = mock_histogram
        infra_recorder._http_errors_total = mock_errors

        with pytest.raises(ValueError):
            with infra_recorder.http_request_timer("GET", "/api/v1/fail"):
                raise ValueError("test error")

        # Duration should still be recorded
        mock_histogram.labels().observe.assert_called_once()
        # Error should be recorded via record_http_error
        mock_errors.labels.assert_called_once_with(
            method="GET", endpoint="/api/v1/fail", error_type="ValueError"
        )

    def test_set_info_with_none_info_is_noop(self, infra_recorder):
        """set_info with no Info metric (None) is a no-op."""
        infra_recorder._info = None

        # Should not raise
        infra_recorder.set_info({"version": "1.0.0"})

    def test_set_info_with_info_metric_calls_info(self, infra_recorder):
        """set_info with Info metric delegates to info()."""
        mock_info = MagicMock()
        infra_recorder._info = mock_info

        infra_recorder.set_info({"version": "1.0.0", "env": "production"})

        mock_info.info.assert_called_once_with(
            {"version": "1.0.0", "env": "production"}
        )

    def test_record_http_request_no_raise_on_internal_error(self, infra_recorder):
        """record_http_request does not raise on internal errors."""
        mock_counter = MagicMock()
        mock_counter.labels.side_effect = RuntimeError("metric error")
        infra_recorder._http_requests_total = mock_counter

        # Should not raise
        infra_recorder.record_http_request("GET", "/fail", 500, 1.0)

    def test_set_request_queue_depth_clamps_negative(self, infra_recorder):
        """set_request_queue_depth clamps negative values to 0."""
        mock_gauge = MagicMock()
        infra_recorder._queue_depth = mock_gauge

        infra_recorder.set_request_queue_depth("api-service", -10)

        mock_gauge.labels.assert_called_once_with(service="api-service")
        mock_gauge.labels().set.assert_called_once_with(0.0)

    def test_set_error_rate_clamps_percentage(self, infra_recorder):
        """set_error_rate clamps value to 0-100 range."""
        mock_gauge = MagicMock()
        infra_recorder._error_rate_percent = mock_gauge

        infra_recorder.set_error_rate("api-service", 150.0)

        mock_gauge.labels().set.assert_called_once_with(100.0)

    def test_record_security_incident_increments_counter(self, infra_recorder):
        """record_security_incident increments _security_incidents."""
        mock_counter = MagicMock()
        infra_recorder._security_incidents = mock_counter

        infra_recorder.record_security_incident("unauthorized_access", "high")

        mock_counter.labels.assert_called_once_with(
            incident_type="unauthorized_access", severity="high"
        )
        mock_counter.labels().inc.assert_called_once()

    def test_record_di_fallback_increments_counter(self, infra_recorder):
        """record_di_fallback increments _di_fallback_total."""
        mock_counter = MagicMock()
        infra_recorder._di_fallback_total = mock_counter

        infra_recorder.record_di_fallback("redis_cache", "memory_adapter")

        mock_counter.labels.assert_called_once_with(
            service="redis_cache", adapter="memory_adapter"
        )
        mock_counter.labels().inc.assert_called_once()

    def test_set_mesh_overrides_active_sets_gauge(self, infra_recorder):
        """set_mesh_overrides_active sets the gauge value."""
        mock_gauge = MagicMock()
        infra_recorder._mesh_overrides_active = mock_gauge

        infra_recorder.set_mesh_overrides_active(3)

        mock_gauge.set.assert_called_once_with(3)

    def test_record_capacity_warmup_increments_counter(self, infra_recorder):
        """record_capacity_warmup increments _capacity_warmup_total."""
        mock_counter = MagicMock()
        infra_recorder._capacity_warmup_total = mock_counter

        infra_recorder.record_capacity_warmup("event-123", "success")

        mock_counter.labels.assert_called_once_with(
            event_id="event-123", outcome="success"
        )
        mock_counter.labels().inc.assert_called_once()


# =============================================================================
# Cross-cutting: Inheritance Contract
# =============================================================================


class TestRecorderInheritanceContract:
    """All recorders inherit from BaseMetricRecorder."""

    def test_dlq_recorder_inherits_base(self):
        """DLQMetricRecorder inherits BaseMetricRecorder."""
        from baldur.metrics.recorders.base import BaseMetricRecorder
        from baldur.metrics.recorders.dlq import DLQMetricRecorder

        assert issubclass(DLQMetricRecorder, BaseMetricRecorder)

    def test_cb_recorder_inherits_base(self):
        """CBMetricRecorder inherits BaseMetricRecorder."""
        from baldur.metrics.recorders.base import BaseMetricRecorder
        from baldur.metrics.recorders.circuit_breaker import CBMetricRecorder

        assert issubclass(CBMetricRecorder, BaseMetricRecorder)

    def test_retry_recorder_inherits_base(self):
        """RetryMetricRecorder inherits BaseMetricRecorder."""
        from baldur.metrics.recorders.base import BaseMetricRecorder
        from baldur.metrics.recorders.retry import RetryMetricRecorder

        assert issubclass(RetryMetricRecorder, BaseMetricRecorder)

    def test_replay_recorder_inherits_base(self):
        """ReplayMetricRecorder inherits BaseMetricRecorder."""
        from baldur.metrics.recorders.base import BaseMetricRecorder
        from baldur.metrics.recorders.replay import ReplayMetricRecorder

        assert issubclass(ReplayMetricRecorder, BaseMetricRecorder)

    def test_infra_recorder_inherits_base(self):
        """InfraMetricRecorder inherits BaseMetricRecorder."""
        from baldur.metrics.recorders.base import BaseMetricRecorder
        from baldur.metrics.recorders.infrastructure import InfraMetricRecorder

        assert issubclass(InfraMetricRecorder, BaseMetricRecorder)
