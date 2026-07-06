"""
Tests for reconciler / metric-sync gauge hydration restoration (647).

Covers:
- D1: ``sync_all_gauges`` / ``sync_domain_gauges`` repoint the hydration writes
  to the recorder public methods (``set_pending_count`` / ``set_state`` /
  ``set_success_rate``), so the gauges reflect the data-source values instead
  of staying at 0.
- D4: the cross-backend gauge-read accessor (``get_pending_count`` /
  ``_GaugeStore.get``) used by the drift-before snapshot.

Parametrized over both metrics backends ``{prometheus, OTEL}`` (D6): the whole
bug class was "dead on both backends", so parity is the bar. Fixtures are
function-scoped to avoid xdist singleton leakage.
"""

from unittest.mock import MagicMock, patch

import pytest

from baldur.metrics.prometheus import (
    BaldurMetrics,
    configure_metrics,
    reset_metrics,
)
from baldur.metrics.reconciler import MetricReconciler

# =============================================================================
# Fixtures / helpers
# =============================================================================


@pytest.fixture(params=["prometheus", "otel"])
def backend(request):
    """Configure the metrics singleton with each backend, then reset.

    OTEL needs the meter patched during construction (the instruments are
    created at init; the ``_GaugeStore`` read/write paths are pure-Python and
    independent of the meter, so the patch can be released afterward).
    """
    if request.param == "prometheus":
        instance = BaldurMetrics()
    else:
        from baldur.metrics.otel_backend import OTELBaldurMetrics

        with patch("baldur.observability.get_meter", return_value=MagicMock()):
            instance = OTELBaldurMetrics()
        assert instance._initialized
    configure_metrics(instance)
    yield request.param, instance
    reset_metrics()


def _read_cb_state(param, instance, service, cell_id=""):
    """Read the circuit-breaker state gauge value across both backends.

    648 D2 swapped the OTel backend's cb family to the reused prometheus
    CBMetricRecorder, so both params now read the same prometheus gauge child
    (``param`` retained for call-site symmetry with ``_read_retry_rate``).
    """
    return instance.circuit_breaker._state.labels(
        service=service, cell_id=cell_id
    )._value.get()


def _read_retry_rate(param, instance, domain):
    """Read the retry-success-rate gauge value across both backends."""
    if param == "prometheus":
        return instance.retry._success_rate.labels(domain=domain)._value.get()
    return instance.retry._success_store.get({"domain": domain})


# =============================================================================
# D1 — sync_all_gauges hydration
# =============================================================================


class TestSyncAllGaugesHydration:
    """sync_all_gauges hydrates all three gauges to the adapter's values."""

    def test_sync_all_gauges_hydrates_all_three(self, backend):
        """All three gauges reflect the adapter values (not 0) on both backends."""
        param, instance = backend
        adapter = MagicMock()
        adapter.get_dlq_pending_count.return_value = 7
        adapter.get_circuit_breaker_state.return_value = "open"
        adapter.get_retry_success_rate.return_value = 88.0

        reconciler = MetricReconciler(
            adapter=adapter,
            domains=["payment"],
            services=["billing_service"],
        )
        result = reconciler.sync_all_gauges()

        # SyncResult still carries the clamped values (belt-and-suspenders).
        assert result.dlq_pending["payment"] == 7
        assert result.circuit_breaker_states["billing_service"] == "open"
        assert result.retry_success_rates["payment"] == 88.0

        # The gauges themselves are hydrated (the formerly-dead write).
        assert instance.dlq.get_pending_count("payment") == 7
        assert _read_cb_state(param, instance, "billing_service") == 1  # open -> 1
        assert _read_retry_rate(param, instance, "payment") == 88.0

    def test_sync_all_gauges_hydrates_composite_cb_name(self, backend):
        """A composite CB key is split into (service, cell_id) at the call site."""
        param, instance = backend
        from baldur.core.cb_namespace import COMPOSITE_KEY_SEPARATOR

        composite = f"orders{COMPOSITE_KEY_SEPARATOR}cell-a"
        adapter = MagicMock()
        adapter.get_dlq_pending_count.return_value = 0
        adapter.get_circuit_breaker_state.return_value = "half_open"
        adapter.get_retry_success_rate.return_value = 100.0

        reconciler = MetricReconciler(
            adapter=adapter,
            domains=["orders"],
            services=[composite],
        )
        reconciler.sync_all_gauges()

        # half_open -> 2, keyed by the split base service + cell_id.
        assert _read_cb_state(param, instance, "orders", "cell-a") == 2


# =============================================================================
# D1 — sync_domain_gauges hydration
# =============================================================================


class TestSyncDomainGaugesHydration:
    """sync_domain_gauges (manual-sync API path) hydrates dlq + retry."""

    def test_sync_domain_gauges_hydrates_dlq_and_retry(self, backend):
        param, instance = backend
        adapter = MagicMock()
        adapter.get_dlq_pending_count.return_value = 3
        adapter.get_retry_success_rate.return_value = 72.0

        reconciler = MetricReconciler(adapter=adapter, domains=["orders"])
        out = reconciler.sync_domain_gauges("orders")

        assert out["dlq_pending"] == 3
        assert out["retry_rate"] == 72.0
        assert instance.dlq.get_pending_count("orders") == 3
        assert _read_retry_rate(param, instance, "orders") == 72.0


# =============================================================================
# D4 — cross-backend gauge-read accessor
# =============================================================================


class TestGaugeReadAccessor:
    """The read accessor returns the current in-memory gauge value."""

    def test_read_accessor_returns_current_value(self, backend):
        """get_pending_count reflects the last set value on both backends."""
        _param, instance = backend
        instance.dlq.set_pending_count("accessor_domain", 42)
        assert instance.dlq.get_pending_count("accessor_domain") == 42

    def test_read_accessor_gauge_store_get_otel(self):
        """_GaugeStore.get returns the stored value (OTEL backend store)."""
        from baldur.metrics.otel_backend import _GaugeStore

        store = _GaugeStore()
        store.set(9.0, {"domain": "x"})
        assert store.get({"domain": "x"}) == 9.0
        # inc/dec then read back through the same accessor.
        store.inc(1.0, {"domain": "x"})
        assert store.get({"domain": "x"}) == 10.0


class TestGaugeReadAccessorFailOpen:
    """DLQMetricRecorder.get_pending_count fails open on a prometheus read error.

    The read uses the prometheus-private ``_value.get()`` (no public read API);
    if a prometheus_client upgrade breaks that path the recorder must fail open
    to 0.0 and surface a WARNING rather than propagate — the drift-before
    snapshot reads through this, so a raise here would crash the drift report.
    OTEL has no equivalent branch (it delegates to ``_GaugeStore.get`` over a
    plain dict), so this behavior is prometheus-only.
    """

    def test_get_pending_count_read_error_returns_zero_and_logs(self):
        # Given: a recorder whose prometheus child-gauge read raises
        import baldur.metrics.recorders.dlq as dlq_module
        from baldur.metrics.recorders.dlq import DLQMetricRecorder

        recorder = DLQMetricRecorder()

        # When: the private _value read fails (simulated via labels() raising)
        with (
            patch.object(
                recorder._pending_gauge,
                "labels",
                side_effect=RuntimeError("gauge read broke"),
            ),
            patch.object(dlq_module, "logger") as mock_logger,
        ):
            result = recorder.get_pending_count("payment")

        # Then: fail open to 0.0 and emit the _failed WARNING (not silent)
        assert result == 0.0
        mock_logger.warning.assert_called_once()
        (event,) = mock_logger.warning.call_args.args
        assert event == "metrics.get_dlq_pending_failed"


# =============================================================================
# SC5 — before-first-hydration boundary
# =============================================================================


class TestBeforeFirstHydration:
    """A never-set gauge key reads 0.0 (treated as real drift, not masked)."""

    def test_before_first_hydration_returns_zero_both_backends(self, backend):
        """get_pending_count returns 0.0 for a never-set domain key."""
        _param, instance = backend
        # A domain no other test labels -> deterministic 0.0 on the shared
        # prometheus registry and a fresh OTEL _GaugeStore alike.
        assert instance.dlq.get_pending_count("never_hydrated_647") == 0.0

    def test_before_first_hydration_gauge_store_default_zero(self):
        """_GaugeStore.get default is 0.0 for an unset key."""
        from baldur.metrics.otel_backend import _GaugeStore

        store = _GaugeStore()
        assert store.get({"domain": "missing"}) == 0.0
