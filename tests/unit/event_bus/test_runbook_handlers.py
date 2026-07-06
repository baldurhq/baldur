"""
Runbook Approval Default Event Handler Tests (379).

Tests for:
1. _on_runbook_approval_required — log (INFO) + metrics
2. _on_runbook_approval_granted — log (INFO) + metrics
3. _on_runbook_approval_rejected — log (WARNING) + metrics
4. _get_runbook_handler_counter — Lazy singleton
5. Handler registration — correct priorities
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from baldur.services.event_bus import BaldurEvent, EventType
from baldur.services.event_bus.bus._runbook_handlers import (
    _get_runbook_handler_counter,
    _on_runbook_approval_granted,
    _on_runbook_approval_rejected,
    _on_runbook_approval_required,
)


def _make_event(event_type: EventType, data: dict, source: str = "test") -> BaldurEvent:
    return BaldurEvent(event_type=event_type, data=data, source=source)


# =============================================================================
# Contract Tests — 379 Document-specified values
# =============================================================================


class TestRunbookHandlerContract:
    """Contract verification for 379 runbook approval handlers."""

    def test_approval_required_event_name(self):
        """Event name: event_bus.runbook_approval_required (379 Event Names)."""
        with (
            patch(
                "baldur.services.event_bus.bus._runbook_handlers.logger",
            ) as mock_logger,
            patch(
                "baldur.services.event_bus.bus._runbook_handlers._get_runbook_handler_counter",
            ),
        ):
            _on_runbook_approval_required(
                _make_event(EventType.RUNBOOK_APPROVAL_REQUIRED, data={})
            )
            event_names = [c.args[0] for c in mock_logger.info.call_args_list]
            assert "event_bus.runbook_approval_required" in event_names

    def test_approval_granted_event_name(self):
        """Event name: event_bus.runbook_approval_granted."""
        with (
            patch(
                "baldur.services.event_bus.bus._runbook_handlers.logger",
            ) as mock_logger,
            patch(
                "baldur.services.event_bus.bus._runbook_handlers._get_runbook_handler_counter",
            ),
        ):
            _on_runbook_approval_granted(
                _make_event(EventType.RUNBOOK_APPROVAL_GRANTED, data={})
            )
            event_names = [c.args[0] for c in mock_logger.info.call_args_list]
            assert "event_bus.runbook_approval_granted" in event_names

    def test_approval_rejected_event_name_and_level(self):
        """Event name: event_bus.runbook_approval_rejected at WARNING level."""
        with (
            patch(
                "baldur.services.event_bus.bus._runbook_handlers.logger",
            ) as mock_logger,
            patch(
                "baldur.services.event_bus.bus._runbook_handlers._get_runbook_handler_counter",
            ),
        ):
            _on_runbook_approval_rejected(
                _make_event(EventType.RUNBOOK_APPROVAL_REJECTED, data={})
            )

            # WARNING level, not INFO
            mock_logger.info.assert_not_called()
            event_names = [c.args[0] for c in mock_logger.warning.call_args_list]
            assert "event_bus.runbook_approval_rejected" in event_names

    def test_metrics_counter_name_and_labels(self):
        """Counter: baldur_runbook_event_handled_total, label: event_type."""
        import baldur.services.event_bus.bus._runbook_handlers as mod

        original = mod._runbook_handler_counter
        mod._runbook_handler_counter = None

        try:
            with patch(
                "baldur.metrics.registry.get_or_create_counter",
            ) as mock_create:
                mock_create.return_value = MagicMock()
                _get_runbook_handler_counter()

                mock_create.assert_called_once_with(
                    "baldur_runbook_event_handled_total",
                    "Total runbook events handled by default handlers",
                    ["event_type"],
                )
        finally:
            mod._runbook_handler_counter = original

    def test_handler_registration_priorities(self):
        """All runbook approval handlers registered at NORMAL priority."""
        from baldur.services.event_bus import get_event_bus
        from baldur.services.event_bus.bus.default_handlers import (
            register_default_handlers,
        )
        from baldur.services.event_bus.bus.event_types import EventPriority

        bus = get_event_bus()
        bus.reset()

        try:
            register_default_handlers()

            for event_type, handler_name in [
                (EventType.RUNBOOK_APPROVAL_REQUIRED, "_on_runbook_approval_required"),
                (EventType.RUNBOOK_APPROVAL_GRANTED, "_on_runbook_approval_granted"),
                (EventType.RUNBOOK_APPROVAL_REJECTED, "_on_runbook_approval_rejected"),
            ]:
                subs = bus.get_subscriptions(event_type)
                handler = next(s for s in subs if s["handler_name"] == handler_name)
                assert handler["priority"] == EventPriority.NORMAL.name
        finally:
            bus.reset()


# =============================================================================
# Behavior Tests — Data extraction, metrics increment
# =============================================================================


class TestRunbookHandlerBehavior:
    """Behavior verification for runbook approval handlers."""

    def test_approval_required_extracts_data_fields(self):
        """Handler extracts runbook_id, runbook_name, execution_id, risk_level."""
        with (
            patch(
                "baldur.services.event_bus.bus._runbook_handlers.logger",
            ) as mock_logger,
            patch(
                "baldur.services.event_bus.bus._runbook_handlers._get_runbook_handler_counter",
            ),
        ):
            event = _make_event(
                EventType.RUNBOOK_APPROVAL_REQUIRED,
                data={
                    "runbook_id": "rb-1",
                    "runbook_name": "restart_service",
                    "execution_id": "exec-42",
                    "risk_level": "high",
                },
            )
            _on_runbook_approval_required(event)

            mock_logger.info.assert_called_once_with(
                "event_bus.runbook_approval_required",
                runbook_id="rb-1",
                runbook_name="restart_service",
                execution_id="exec-42",
                risk_level="high",
            )

    def test_approval_granted_extracts_approved_by(self):
        """Handler extracts execution_id and approved_by."""
        with (
            patch(
                "baldur.services.event_bus.bus._runbook_handlers.logger",
            ) as mock_logger,
            patch(
                "baldur.services.event_bus.bus._runbook_handlers._get_runbook_handler_counter",
            ),
        ):
            event = _make_event(
                EventType.RUNBOOK_APPROVAL_GRANTED,
                data={"execution_id": "exec-1", "approved_by": "admin@co"},
            )
            _on_runbook_approval_granted(event)

            mock_logger.info.assert_called_once_with(
                "event_bus.runbook_approval_granted",
                execution_id="exec-1",
                approved_by="admin@co",
            )

    def test_approval_rejected_extracts_reason(self):
        """Handler extracts execution_id, rejected_by, reason."""
        with (
            patch(
                "baldur.services.event_bus.bus._runbook_handlers.logger",
            ) as mock_logger,
            patch(
                "baldur.services.event_bus.bus._runbook_handlers._get_runbook_handler_counter",
            ),
        ):
            event = _make_event(
                EventType.RUNBOOK_APPROVAL_REJECTED,
                data={
                    "execution_id": "exec-1",
                    "rejected_by": "sre@co",
                    "reason": "too risky",
                },
            )
            _on_runbook_approval_rejected(event)

            mock_logger.warning.assert_called_once_with(
                "event_bus.runbook_approval_rejected",
                execution_id="exec-1",
                rejected_by="sre@co",
                reason="too risky",
            )

    def test_approval_required_increments_metrics(self):
        """Handler increments counter with event_type=approval_required."""
        with (
            patch(
                "baldur.services.event_bus.bus._runbook_handlers.logger",
            ),
            patch(
                "baldur.services.event_bus.bus._runbook_handlers._get_runbook_handler_counter",
            ) as mock_get,
        ):
            mock_counter = MagicMock()
            mock_get.return_value = mock_counter

            _on_runbook_approval_required(
                _make_event(EventType.RUNBOOK_APPROVAL_REQUIRED, data={})
            )

            mock_counter.labels.assert_called_with(event_type="approval_required")
            mock_counter.labels.return_value.inc.assert_called_once()

    def test_metrics_failure_does_not_propagate(self):
        """Metrics exception is swallowed — handler doesn't raise."""
        with (
            patch(
                "baldur.services.event_bus.bus._runbook_handlers.logger",
            ),
            patch(
                "baldur.services.event_bus.bus._runbook_handlers._get_runbook_handler_counter",
            ) as mock_get,
        ):
            mock_get.return_value.labels.side_effect = RuntimeError("prom down")

            # Should not raise
            _on_runbook_approval_required(
                _make_event(EventType.RUNBOOK_APPROVAL_REQUIRED, data={})
            )

    def test_handler_handles_none_data(self):
        """Handler works when event.data is None."""
        with (
            patch(
                "baldur.services.event_bus.bus._runbook_handlers.logger",
            ) as mock_logger,
            patch(
                "baldur.services.event_bus.bus._runbook_handlers._get_runbook_handler_counter",
            ),
        ):
            event = MagicMock()
            event.data = None

            _on_runbook_approval_required(event)

            mock_logger.info.assert_called_once_with(
                "event_bus.runbook_approval_required",
                runbook_id=None,
                runbook_name=None,
                execution_id=None,
                risk_level=None,
            )

    def test_metrics_counter_singleton_caches(self):
        """Counter is created once and cached on subsequent calls."""
        import baldur.services.event_bus.bus._runbook_handlers as mod

        original = mod._runbook_handler_counter
        mod._runbook_handler_counter = None

        try:
            with patch(
                "baldur.metrics.registry.get_or_create_counter",
            ) as mock_create:
                mock_create.return_value = MagicMock()

                first = _get_runbook_handler_counter()
                second = _get_runbook_handler_counter()

                assert first is second
                mock_create.assert_called_once()
        finally:
            mod._runbook_handler_counter = original
