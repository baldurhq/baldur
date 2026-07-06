"""
Saga Default Event Handler Tests (379).

Tests for:
1. _on_saga_timed_out — 3-piece (log + audit + metrics) with feature toggle
2. _on_saga_compensation_failed — 3-piece with failed_steps parsing
3. _on_saga_completed — log + metrics with feature toggle
4. _on_saga_compensated — log + metrics with feature toggle
5. _get_saga_handler_counter — Lazy singleton metrics counter
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from baldur.services.event_bus import BaldurEvent, EventType
from baldur.services.event_bus.bus._saga_handlers import (
    _get_saga_handler_counter,
    _on_saga_compensated,
    _on_saga_compensation_failed,
    _on_saga_completed,
    _on_saga_timed_out,
)


def _make_event(event_type: EventType, data: dict, source: str = "test") -> BaldurEvent:
    return BaldurEvent(event_type=event_type, data=data, source=source)


# =============================================================================
# Contract Tests — 379 Document-specified values
# =============================================================================


class TestSagaHandlerContract:
    """Contract verification for 379 saga event handlers."""

    def test_timed_out_event_name_uses_event_bus_prefix(self):
        """Event name must be event_bus.saga_timed_out_handled (379 Event Names)."""
        pytest.importorskip("baldur_pro")
        with (
            patch(
                "baldur.services.event_bus.bus._saga_handlers.logger",
            ) as mock_logger,
            patch(
                "baldur.settings.saga.get_saga_settings",
            ) as mock_settings,
            patch(
                "baldur.services.event_bus.bus._saga_handlers._get_saga_handler_counter",
            ),
            patch(
                "baldur_pro.services.audit.saga_audit._write_to_wal",
                return_value=1,
            ),
        ):
            mock_settings.return_value = MagicMock(enabled=True)
            event = _make_event(EventType.SAGA_TIMED_OUT, data={})
            _on_saga_timed_out(event)

            event_names = [call.args[0] for call in mock_logger.warning.call_args_list]
            assert "event_bus.saga_timed_out_handled" in event_names

    def test_compensation_failed_event_name(self):
        """Event name must be event_bus.saga_compensation_failed_handled."""
        pytest.importorskip("baldur_pro")
        with (
            patch(
                "baldur.services.event_bus.bus._saga_handlers.logger",
            ) as mock_logger,
            patch(
                "baldur.settings.saga.get_saga_settings",
            ) as mock_settings,
            patch(
                "baldur.services.event_bus.bus._saga_handlers._get_saga_handler_counter",
            ),
            patch(
                "baldur_pro.services.audit.saga_audit._write_to_wal",
                return_value=1,
            ),
        ):
            mock_settings.return_value = MagicMock(enabled=True)
            event = _make_event(EventType.SAGA_COMPENSATION_FAILED, data={})
            _on_saga_compensation_failed(event)

            event_names = [call.args[0] for call in mock_logger.warning.call_args_list]
            assert "event_bus.saga_compensation_failed_handled" in event_names

    def test_completed_event_name(self):
        """Event name must be event_bus.saga_completed_handled."""
        with (
            patch(
                "baldur.services.event_bus.bus._saga_handlers.logger",
            ) as mock_logger,
            patch(
                "baldur.settings.saga.get_saga_settings",
            ) as mock_settings,
            patch(
                "baldur.services.event_bus.bus._saga_handlers._get_saga_handler_counter",
            ),
        ):
            mock_settings.return_value = MagicMock(enabled=True)
            event = _make_event(EventType.SAGA_COMPLETED, data={})
            _on_saga_completed(event)

            event_names = [call.args[0] for call in mock_logger.info.call_args_list]
            assert "event_bus.saga_completed_handled" in event_names

    def test_compensated_event_name(self):
        """Event name must be event_bus.saga_compensated_handled."""
        with (
            patch(
                "baldur.services.event_bus.bus._saga_handlers.logger",
            ) as mock_logger,
            patch(
                "baldur.settings.saga.get_saga_settings",
            ) as mock_settings,
            patch(
                "baldur.services.event_bus.bus._saga_handlers._get_saga_handler_counter",
            ),
        ):
            mock_settings.return_value = MagicMock(enabled=True)
            event = _make_event(EventType.SAGA_COMPENSATED, data={})
            _on_saga_compensated(event)

            event_names = [call.args[0] for call in mock_logger.info.call_args_list]
            assert "event_bus.saga_compensated_handled" in event_names

    def test_metrics_counter_name_and_labels(self):
        """Counter name: baldur_saga_event_handled_total, label: status."""
        import baldur.services.event_bus.bus._saga_handlers as mod

        original = mod._saga_handler_counter
        mod._saga_handler_counter = None

        try:
            with patch(
                "baldur.metrics.registry.get_or_create_counter",
            ) as mock_create:
                mock_counter = MagicMock()
                mock_create.return_value = mock_counter

                result = _get_saga_handler_counter()

                mock_create.assert_called_once_with(
                    "baldur_saga_event_handled_total",
                    "Total saga events handled by default handlers",
                    ["status"],
                )
                assert result is mock_counter
        finally:
            mod._saga_handler_counter = original

    def test_handler_registration_priorities(self):
        """Saga handlers registered with correct priorities per 379."""
        from baldur.services.event_bus import get_event_bus
        from baldur.services.event_bus.bus.default_handlers import (
            register_default_handlers,
        )
        from baldur.services.event_bus.bus.event_types import EventPriority

        bus = get_event_bus()
        bus.reset()

        try:
            register_default_handlers()

            # SAGA_TIMED_OUT → CRITICAL
            timed_out_subs = bus.get_subscriptions(EventType.SAGA_TIMED_OUT)
            timed_out = next(
                s for s in timed_out_subs if s["handler_name"] == "_on_saga_timed_out"
            )
            assert timed_out["priority"] == EventPriority.CRITICAL.name

            # SAGA_COMPENSATION_FAILED → CRITICAL
            comp_fail_subs = bus.get_subscriptions(EventType.SAGA_COMPENSATION_FAILED)
            comp_fail = next(
                s
                for s in comp_fail_subs
                if s["handler_name"] == "_on_saga_compensation_failed"
            )
            assert comp_fail["priority"] == EventPriority.CRITICAL.name

            # SAGA_COMPLETED → NORMAL
            completed_subs = bus.get_subscriptions(EventType.SAGA_COMPLETED)
            completed = next(
                s for s in completed_subs if s["handler_name"] == "_on_saga_completed"
            )
            assert completed["priority"] == EventPriority.NORMAL.name

            # SAGA_COMPENSATED → NORMAL
            compensated_subs = bus.get_subscriptions(EventType.SAGA_COMPENSATED)
            compensated = next(
                s
                for s in compensated_subs
                if s["handler_name"] == "_on_saga_compensated"
            )
            assert compensated["priority"] == EventPriority.NORMAL.name
        finally:
            bus.reset()


# =============================================================================
# Behavior Tests — Feature toggle, side effects, data extraction
# =============================================================================


class TestSagaHandlerBehavior:
    """Behavior verification for saga event handlers."""

    def test_timed_out_skips_when_disabled(self):
        """Feature toggle: handler returns immediately when saga disabled."""
        with (
            patch(
                "baldur.services.event_bus.bus._saga_handlers.logger",
            ) as mock_logger,
            patch(
                "baldur.settings.saga.get_saga_settings",
            ) as mock_settings,
        ):
            mock_settings.return_value = MagicMock(enabled=False)
            event = _make_event(
                EventType.SAGA_TIMED_OUT,
                data={"saga_name": "test", "instance_id": "id-1"},
            )
            _on_saga_timed_out(event)

            mock_logger.warning.assert_not_called()

    def test_completed_skips_when_disabled(self):
        """Feature toggle: completed handler skips when disabled."""
        with (
            patch(
                "baldur.services.event_bus.bus._saga_handlers.logger",
            ) as mock_logger,
            patch(
                "baldur.settings.saga.get_saga_settings",
            ) as mock_settings,
        ):
            mock_settings.return_value = MagicMock(enabled=False)
            _on_saga_completed(_make_event(EventType.SAGA_COMPLETED, data={}))
            mock_logger.info.assert_not_called()

    def test_timed_out_calls_audit(self):
        """Failure handler calls log_saga_timeout_audit with correct args."""
        pytest.importorskip("baldur_pro")
        with (
            patch(
                "baldur.services.event_bus.bus._saga_handlers.logger",
            ),
            patch(
                "baldur.settings.saga.get_saga_settings",
            ) as mock_settings,
            patch(
                "baldur.services.event_bus.bus._saga_handlers._get_saga_handler_counter",
            ),
            patch(
                "baldur_pro.services.audit.saga_audit._write_to_wal",
                return_value=1,
            ) as mock_wal,
        ):
            mock_settings.return_value = MagicMock(enabled=True)
            event = _make_event(
                EventType.SAGA_TIMED_OUT,
                data={
                    "saga_name": "order_saga",
                    "instance_id": "inst-42",
                    "timeout_seconds": 600,
                },
            )
            _on_saga_timed_out(event)

            mock_wal.assert_called_once()
            call_kwargs = mock_wal.call_args[1]
            assert call_kwargs["event_type"] == "SAGA_TIMED_OUT"
            assert call_kwargs["domain"] == "saga"
            assert call_kwargs["target_id"] == "inst-42"

    def test_compensation_failed_calls_audit_with_all_failed_steps(self):
        """Compensation failed handler passes all failed steps to audit."""
        pytest.importorskip("baldur_pro")
        with (
            patch(
                "baldur.services.event_bus.bus._saga_handlers.logger",
            ),
            patch(
                "baldur.settings.saga.get_saga_settings",
            ) as mock_settings,
            patch(
                "baldur.services.event_bus.bus._saga_handlers._get_saga_handler_counter",
            ),
            patch(
                "baldur_pro.services.audit.saga_audit._write_to_wal",
                return_value=1,
            ) as mock_wal,
        ):
            mock_settings.return_value = MagicMock(enabled=True)
            event = _make_event(
                EventType.SAGA_COMPENSATION_FAILED,
                data={
                    "saga_name": "order_saga",
                    "instance_id": "inst-42",
                    "failed_steps": ["step_payment", "step_inventory"],
                    "original_failure_reason": "timeout",
                },
            )
            _on_saga_compensation_failed(event)

            mock_wal.assert_called_once()
            details = mock_wal.call_args[1]["details"]
            assert details["failed_steps"] == ["step_payment", "step_inventory"]
            assert details["error_message"] == "timeout"

    def test_timed_out_increments_metrics(self):
        """Failure handler increments counter with status=timed_out."""
        pytest.importorskip("baldur_pro")
        with (
            patch(
                "baldur.services.event_bus.bus._saga_handlers.logger",
            ),
            patch(
                "baldur.settings.saga.get_saga_settings",
            ) as mock_settings,
            patch(
                "baldur.services.event_bus.bus._saga_handlers._get_saga_handler_counter",
            ) as mock_get_counter,
            patch(
                "baldur_pro.services.audit.saga_audit._write_to_wal",
                return_value=1,
            ),
        ):
            mock_settings.return_value = MagicMock(enabled=True)
            mock_counter = MagicMock()
            mock_get_counter.return_value = mock_counter

            _on_saga_timed_out(_make_event(EventType.SAGA_TIMED_OUT, data={}))

            mock_counter.labels.assert_called_with(status="timed_out")
            mock_counter.labels.return_value.inc.assert_called_once()

    def test_completed_increments_metrics(self):
        """Completed handler increments counter with status=completed."""
        with (
            patch(
                "baldur.services.event_bus.bus._saga_handlers.logger",
            ),
            patch(
                "baldur.settings.saga.get_saga_settings",
            ) as mock_settings,
            patch(
                "baldur.services.event_bus.bus._saga_handlers._get_saga_handler_counter",
            ) as mock_get_counter,
        ):
            mock_settings.return_value = MagicMock(enabled=True)
            mock_counter = MagicMock()
            mock_get_counter.return_value = mock_counter

            _on_saga_completed(_make_event(EventType.SAGA_COMPLETED, data={}))

            mock_counter.labels.assert_called_with(status="completed")
            mock_counter.labels.return_value.inc.assert_called_once()

    def test_timed_out_defaults_unknown_when_data_missing(self):
        """Handler defaults to 'unknown' when saga_name/instance_id not in data."""
        pytest.importorskip("baldur_pro")
        with (
            patch(
                "baldur.services.event_bus.bus._saga_handlers.logger",
            ) as mock_logger,
            patch(
                "baldur.settings.saga.get_saga_settings",
            ) as mock_settings,
            patch(
                "baldur.services.event_bus.bus._saga_handlers._get_saga_handler_counter",
            ),
            patch(
                "baldur_pro.services.audit.saga_audit._write_to_wal",
                return_value=1,
            ),
        ):
            mock_settings.return_value = MagicMock(enabled=True)
            _on_saga_timed_out(_make_event(EventType.SAGA_TIMED_OUT, data={}))

            mock_logger.warning.assert_any_call(
                "event_bus.saga_timed_out_handled",
                saga_name="unknown",
                instance_id="unknown",
                timeout_seconds=None,
            )

    def test_timed_out_audit_failure_does_not_propagate(self):
        """Audit failure does not prevent metric increment (Fail-Open).

        Post-518-a: fail-open is owned by baldur.audit.helpers._safe_delegate
        rather than a try/except in the caller. This test simulates the
        contract by patching the helper to return None (the fail-open
        result), and verifies the metric is still incremented downstream.
        """
        with (
            patch(
                "baldur.services.event_bus.bus._saga_handlers.logger",
            ),
            patch(
                "baldur.settings.saga.get_saga_settings",
            ) as mock_settings,
            patch(
                "baldur.services.event_bus.bus._saga_handlers._get_saga_handler_counter",
            ) as mock_get_counter,
            patch(
                "baldur.services.event_bus.bus._saga_handlers.log_saga_timeout_audit",
                return_value=None,
            ),
        ):
            mock_settings.return_value = MagicMock(enabled=True)
            mock_counter = MagicMock()
            mock_get_counter.return_value = mock_counter

            _on_saga_timed_out(
                _make_event(EventType.SAGA_TIMED_OUT, data={"saga_name": "s"})
            )

            # Metrics still called
            mock_counter.labels.assert_called_with(status="timed_out")

    def test_metrics_counter_singleton_caches(self):
        """Counter singleton is created once and cached."""
        import baldur.services.event_bus.bus._saga_handlers as mod

        original = mod._saga_handler_counter
        mod._saga_handler_counter = None

        try:
            with patch(
                "baldur.metrics.registry.get_or_create_counter",
            ) as mock_create:
                mock_create.return_value = MagicMock()

                first = _get_saga_handler_counter()
                second = _get_saga_handler_counter()

                assert first is second
                mock_create.assert_called_once()
        finally:
            mod._saga_handler_counter = original
