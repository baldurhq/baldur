"""
CircuitBreakerEventHandler composite-key parsing unit tests.

Target: metrics/event_handlers.py CircuitBreakerEventHandler.on_state_changed()
- Splits a composite key (service::cell_id) into the recorder's service /
  cell_id arguments.
- Legacy keys (no cell_id) pass cell_id="".

The handler routes the state change through the recorder public method
``circuit_breaker.record_state_change(service, from_state, to_state,
cell_id=...)`` (which sets the state gauge and increments the transitions
counter in one call), so the composite-key split is asserted on that call's
arguments (645 D1).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from baldur.core.cb_namespace import (
    make_cell_scoped_cb_name,
)


class TestCircuitBreakerEventHandlerCompositeKeyBehavior:
    """on_state_changed composite-key parsing behavior."""

    def _call_on_state_changed(self, service: str, from_state: str, to_state: str):
        """Invoke on_state_changed and return the mock metrics backend."""
        mock_metrics = MagicMock()

        with patch(
            "baldur.metrics.event_handlers._get_metrics",
            return_value=mock_metrics,
        ):
            from baldur.metrics.event_handlers import (
                CircuitBreakerEventHandler,
            )

            CircuitBreakerEventHandler.on_state_changed(
                service=service,
                from_state=from_state,
                to_state=to_state,
            )
        return mock_metrics

    def test_composite_key_splits_service_and_cell_id(self):
        """A composite key is split into service / cell_id on record_state_change."""
        composite = make_cell_scoped_cb_name("payment_api", "cell-3")
        mock_metrics = self._call_on_state_changed(composite, "closed", "open")

        mock_metrics.circuit_breaker.record_state_change.assert_called_once_with(
            "payment_api",
            "closed",
            "open",
            cell_id="cell-3",
        )

    def test_legacy_key_passes_empty_cell_id(self):
        """A legacy key passes cell_id=''."""
        mock_metrics = self._call_on_state_changed("legacy_svc", "closed", "open")

        mock_metrics.circuit_breaker.record_state_change.assert_called_once_with(
            "legacy_svc",
            "closed",
            "open",
            cell_id="",
        )

    def test_state_change_records_cell_id(self):
        """The state-change call (which drives the transitions counter) carries
        the split cell_id."""
        composite = make_cell_scoped_cb_name("order_api", "cell-7")
        mock_metrics = self._call_on_state_changed(composite, "open", "closed")

        mock_metrics.circuit_breaker.record_state_change.assert_called_once_with(
            "order_api",
            "open",
            "closed",
            cell_id="cell-7",
        )

    def test_no_error_when_metrics_unavailable(self):
        """No exception is raised when metrics are unavailable."""
        with patch(
            "baldur.metrics.event_handlers._get_metrics",
            return_value=None,
        ):
            from baldur.metrics.event_handlers import (
                CircuitBreakerEventHandler,
            )

            # Must return without raising.
            CircuitBreakerEventHandler.on_state_changed(
                service="svc::cell-1",
                from_state="closed",
                to_state="open",
            )

    def _call_on_failure(self, service: str):
        """Invoke on_failure and return the mock metrics backend."""
        mock_metrics = MagicMock()

        with patch(
            "baldur.metrics.event_handlers._get_metrics",
            return_value=mock_metrics,
        ):
            from baldur.metrics.event_handlers import (
                CircuitBreakerEventHandler,
            )

            CircuitBreakerEventHandler.on_failure(service)
        return mock_metrics

    def test_on_failure_splits_composite_key_to_base_service(self):
        """on_failure emits the failures counter on base_service, not the raw
        composite name — keeping the failures series' service label consistent
        with the state / transitions / trips series (the failures counter is
        service-only, so the cell_id is dropped from the metric, like trips)."""
        composite = make_cell_scoped_cb_name("payment_api", "cell-3")
        mock_metrics = self._call_on_failure(composite)

        mock_metrics.circuit_breaker.record_failure.assert_called_once_with(
            "payment_api"
        )

    def test_on_failure_legacy_key_passes_unchanged(self):
        """A legacy (non-composite) key reaches record_failure unchanged."""
        mock_metrics = self._call_on_failure("legacy_svc")

        mock_metrics.circuit_breaker.record_failure.assert_called_once_with(
            "legacy_svc"
        )
