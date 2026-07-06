"""
Chaos Default Event Handler Tests (380).

Tests for:
1. _on_chaos_experiment_blocked — log (WARNING) + metrics with feature toggle
2. _on_chaos_experiment_started — log (DEBUG) + metrics with feature toggle
3. _on_chaos_experiment_stopped — log (INFO/WARNING) + metrics + cardinality guard
4. _get_chaos_handler_counter — Lazy singleton metrics counter
5. _KNOWN_STATUSES — Cardinality guard contract
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from baldur.services.event_bus import BaldurEvent, EventType
from baldur.services.event_bus.bus._chaos_handlers import (
    _KNOWN_STATUSES,
    _get_chaos_handler_counter,
    _on_chaos_experiment_blocked,
    _on_chaos_experiment_started,
    _on_chaos_experiment_stopped,
)

_HANDLER_MODULE = "baldur.services.event_bus.bus._chaos_handlers"


def _make_event(event_type: EventType, data: dict, source: str = "test") -> BaldurEvent:
    return BaldurEvent(event_type=event_type, data=data, source=source)


# =============================================================================
# Contract Tests — 380 Document-specified values
# =============================================================================


class TestChaosHandlerContract:
    """Contract verification for 380 chaos event handlers."""

    def test_blocked_event_name_uses_event_bus_prefix(self):
        """Event name must be event_bus.chaos_experiment_blocked (380 Event Names)."""
        with (
            patch(f"{_HANDLER_MODULE}.logger") as mock_logger,
            patch(
                "baldur.settings.chaos.get_chaos_settings",
            ) as mock_settings,
            patch(f"{_HANDLER_MODULE}._get_chaos_handler_counter"),
        ):
            mock_settings.return_value = MagicMock(enabled=True)
            event = _make_event(EventType.CHAOS_EXPERIMENT_BLOCKED, data={})
            _on_chaos_experiment_blocked(event)

            event_names = [call.args[0] for call in mock_logger.warning.call_args_list]
            assert "event_bus.chaos_experiment_blocked" in event_names

    def test_started_event_name_uses_event_bus_prefix(self):
        """Event name must be event_bus.chaos_experiment_started (380 Event Names)."""
        with (
            patch(f"{_HANDLER_MODULE}.logger") as mock_logger,
            patch(
                "baldur.settings.chaos.get_chaos_settings",
            ) as mock_settings,
            patch(f"{_HANDLER_MODULE}._get_chaos_handler_counter"),
        ):
            mock_settings.return_value = MagicMock(enabled=True)
            event = _make_event(EventType.CHAOS_EXPERIMENT_STARTED, data={})
            _on_chaos_experiment_started(event)

            event_names = [call.args[0] for call in mock_logger.debug.call_args_list]
            assert "event_bus.chaos_experiment_started" in event_names

    def test_stopped_event_name_uses_event_bus_prefix(self):
        """Event name must be event_bus.chaos_experiment_stopped (380 Event Names)."""
        with (
            patch(f"{_HANDLER_MODULE}.logger") as mock_logger,
            patch(
                "baldur.settings.chaos.get_chaos_settings",
            ) as mock_settings,
            patch(f"{_HANDLER_MODULE}._get_chaos_handler_counter"),
        ):
            mock_settings.return_value = MagicMock(enabled=True)
            event = _make_event(
                EventType.CHAOS_EXPERIMENT_STOPPED,
                data={"status": "completed", "success": True},
            )
            _on_chaos_experiment_stopped(event)

            event_names = [call.args[0] for call in mock_logger.info.call_args_list]
            assert "event_bus.chaos_experiment_stopped" in event_names

    def test_metrics_counter_name_and_labels(self):
        """Counter name: baldur_chaos_event_handled_total, label: status."""
        import baldur.services.event_bus.bus._chaos_handlers as mod

        original = mod._chaos_handler_counter
        mod._chaos_handler_counter = None

        try:
            with patch(
                "baldur.metrics.registry.get_or_create_counter",
            ) as mock_create:
                mock_counter = MagicMock()
                mock_create.return_value = mock_counter

                result = _get_chaos_handler_counter()

                mock_create.assert_called_once_with(
                    "baldur_chaos_event_handled_total",
                    "Total chaos events handled by default handlers",
                    ["status"],
                )
                assert result is mock_counter
        finally:
            mod._chaos_handler_counter = original

    def test_known_statuses_contains_expected_members(self):
        """_KNOWN_STATUSES has 9 expected status values (D-14)."""
        expected = {
            "started",
            "completed",
            "failed",
            "aborted",
            "skipped",
            "blocked",
            "rolled_back",
            "recovery_monitoring",
            "error",
        }
        assert _KNOWN_STATUSES == expected

    def test_known_statuses_is_frozenset(self):
        """_KNOWN_STATUSES is a frozenset for immutability."""
        assert isinstance(_KNOWN_STATUSES, frozenset)

    def test_handler_registration_priorities(self):
        """Chaos handlers registered with correct priorities per 380."""
        from baldur.services.event_bus import get_event_bus
        from baldur.services.event_bus.bus.default_handlers import (
            register_default_handlers,
        )
        from baldur.services.event_bus.bus.event_types import EventPriority

        bus = get_event_bus()
        bus.reset()

        try:
            register_default_handlers()

            # BLOCKED → HIGH
            blocked_subs = bus.get_subscriptions(EventType.CHAOS_EXPERIMENT_BLOCKED)
            blocked = next(
                s
                for s in blocked_subs
                if s["handler_name"] == "_on_chaos_experiment_blocked"
            )
            assert blocked["priority"] == EventPriority.HIGH.name

            # STARTED → NORMAL
            started_subs = bus.get_subscriptions(EventType.CHAOS_EXPERIMENT_STARTED)
            started = next(
                s
                for s in started_subs
                if s["handler_name"] == "_on_chaos_experiment_started"
            )
            assert started["priority"] == EventPriority.NORMAL.name

            # STOPPED → NORMAL
            stopped_subs = bus.get_subscriptions(EventType.CHAOS_EXPERIMENT_STOPPED)
            stopped = next(
                s
                for s in stopped_subs
                if s["handler_name"] == "_on_chaos_experiment_stopped"
            )
            assert stopped["priority"] == EventPriority.NORMAL.name
        finally:
            bus.reset()


# =============================================================================
# Behavior Tests — Feature toggle, side effects, cardinality guard
# =============================================================================


class TestChaosHandlerBehavior:
    """Behavior verification for chaos event handlers."""

    # ---- Feature toggle (D-5) ----

    def test_blocked_skips_when_chaos_disabled(self):
        """Feature toggle: blocked handler returns immediately when chaos disabled."""
        with (
            patch(f"{_HANDLER_MODULE}.logger") as mock_logger,
            patch(
                "baldur.settings.chaos.get_chaos_settings",
            ) as mock_settings,
        ):
            mock_settings.return_value = MagicMock(enabled=False)
            _on_chaos_experiment_blocked(
                _make_event(EventType.CHAOS_EXPERIMENT_BLOCKED, data={})
            )
            mock_logger.warning.assert_not_called()

    def test_started_skips_when_chaos_disabled(self):
        """Feature toggle: started handler returns immediately when chaos disabled."""
        with (
            patch(f"{_HANDLER_MODULE}.logger") as mock_logger,
            patch(
                "baldur.settings.chaos.get_chaos_settings",
            ) as mock_settings,
        ):
            mock_settings.return_value = MagicMock(enabled=False)
            _on_chaos_experiment_started(
                _make_event(EventType.CHAOS_EXPERIMENT_STARTED, data={})
            )
            mock_logger.debug.assert_not_called()

    def test_stopped_skips_when_chaos_disabled(self):
        """Feature toggle: stopped handler returns immediately when chaos disabled."""
        with (
            patch(f"{_HANDLER_MODULE}.logger") as mock_logger,
            patch(
                "baldur.settings.chaos.get_chaos_settings",
            ) as mock_settings,
        ):
            mock_settings.return_value = MagicMock(enabled=False)
            _on_chaos_experiment_stopped(
                _make_event(EventType.CHAOS_EXPERIMENT_STOPPED, data={})
            )
            mock_logger.info.assert_not_called()
            mock_logger.warning.assert_not_called()

    # ---- Metrics increment ----

    def test_blocked_increments_counter_with_status_blocked(self):
        """Blocked handler increments counter with status=blocked."""
        with (
            patch(f"{_HANDLER_MODULE}.logger"),
            patch(
                "baldur.settings.chaos.get_chaos_settings",
            ) as mock_settings,
            patch(
                f"{_HANDLER_MODULE}._get_chaos_handler_counter",
            ) as mock_get_counter,
        ):
            mock_settings.return_value = MagicMock(enabled=True)
            mock_counter = MagicMock()
            mock_get_counter.return_value = mock_counter

            _on_chaos_experiment_blocked(
                _make_event(EventType.CHAOS_EXPERIMENT_BLOCKED, data={})
            )

            mock_counter.labels.assert_called_with(status="blocked")
            mock_counter.labels.return_value.inc.assert_called_once()

    def test_started_increments_counter_with_status_started(self):
        """Started handler increments counter with status=started."""
        with (
            patch(f"{_HANDLER_MODULE}.logger"),
            patch(
                "baldur.settings.chaos.get_chaos_settings",
            ) as mock_settings,
            patch(
                f"{_HANDLER_MODULE}._get_chaos_handler_counter",
            ) as mock_get_counter,
        ):
            mock_settings.return_value = MagicMock(enabled=True)
            mock_counter = MagicMock()
            mock_get_counter.return_value = mock_counter

            _on_chaos_experiment_started(
                _make_event(EventType.CHAOS_EXPERIMENT_STARTED, data={})
            )

            mock_counter.labels.assert_called_with(status="started")
            mock_counter.labels.return_value.inc.assert_called_once()

    def test_stopped_increments_counter_with_actual_status(self):
        """Stopped handler increments counter with the actual experiment status."""
        with (
            patch(f"{_HANDLER_MODULE}.logger"),
            patch(
                "baldur.settings.chaos.get_chaos_settings",
            ) as mock_settings,
            patch(
                f"{_HANDLER_MODULE}._get_chaos_handler_counter",
            ) as mock_get_counter,
        ):
            mock_settings.return_value = MagicMock(enabled=True)
            mock_counter = MagicMock()
            mock_get_counter.return_value = mock_counter

            _on_chaos_experiment_stopped(
                _make_event(
                    EventType.CHAOS_EXPERIMENT_STOPPED,
                    data={"status": "completed", "success": True},
                )
            )

            mock_counter.labels.assert_called_with(status="completed")
            mock_counter.labels.return_value.inc.assert_called_once()

    # ---- STOPPED log level (D-12) ----

    def test_stopped_logs_info_when_success(self):
        """STOPPED handler uses logger.info when success=True (D-12)."""
        with (
            patch(f"{_HANDLER_MODULE}.logger") as mock_logger,
            patch(
                "baldur.settings.chaos.get_chaos_settings",
            ) as mock_settings,
            patch(f"{_HANDLER_MODULE}._get_chaos_handler_counter"),
        ):
            mock_settings.return_value = MagicMock(enabled=True)
            _on_chaos_experiment_stopped(
                _make_event(
                    EventType.CHAOS_EXPERIMENT_STOPPED,
                    data={"status": "completed", "success": True},
                )
            )

            mock_logger.info.assert_called_once()
            mock_logger.warning.assert_not_called()

    def test_stopped_logs_info_when_dry_run(self):
        """STOPPED handler uses logger.info when dry_run=True even if success=False (D-12)."""
        with (
            patch(f"{_HANDLER_MODULE}.logger") as mock_logger,
            patch(
                "baldur.settings.chaos.get_chaos_settings",
            ) as mock_settings,
            patch(f"{_HANDLER_MODULE}._get_chaos_handler_counter"),
        ):
            mock_settings.return_value = MagicMock(enabled=True)
            _on_chaos_experiment_stopped(
                _make_event(
                    EventType.CHAOS_EXPERIMENT_STOPPED,
                    data={"status": "failed", "success": False, "dry_run": True},
                )
            )

            mock_logger.info.assert_called_once()
            mock_logger.warning.assert_not_called()

    def test_stopped_logs_warning_when_real_failure(self):
        """STOPPED handler uses logger.warning when success=False and dry_run=False (D-12)."""
        with (
            patch(f"{_HANDLER_MODULE}.logger") as mock_logger,
            patch(
                "baldur.settings.chaos.get_chaos_settings",
            ) as mock_settings,
            patch(f"{_HANDLER_MODULE}._get_chaos_handler_counter"),
        ):
            mock_settings.return_value = MagicMock(enabled=True)
            _on_chaos_experiment_stopped(
                _make_event(
                    EventType.CHAOS_EXPERIMENT_STOPPED,
                    data={"status": "failed", "success": False, "dry_run": False},
                )
            )

            mock_logger.warning.assert_called_once()
            mock_logger.info.assert_not_called()

    # ---- Cardinality guard (D-14) ----

    def test_stopped_replaces_unknown_status_with_unknown_label(self):
        """Unknown status value is replaced with 'unknown' for metrics (D-14)."""
        with (
            patch(f"{_HANDLER_MODULE}.logger"),
            patch(
                "baldur.settings.chaos.get_chaos_settings",
            ) as mock_settings,
            patch(
                f"{_HANDLER_MODULE}._get_chaos_handler_counter",
            ) as mock_get_counter,
        ):
            mock_settings.return_value = MagicMock(enabled=True)
            mock_counter = MagicMock()
            mock_get_counter.return_value = mock_counter

            _on_chaos_experiment_stopped(
                _make_event(
                    EventType.CHAOS_EXPERIMENT_STOPPED,
                    data={"status": "totally_unexpected_status"},
                )
            )

            mock_counter.labels.assert_called_with(status="unknown")

    def test_stopped_passes_known_status_through(self):
        """Known status values pass through the cardinality guard unchanged."""
        for known_status in ("completed", "error", "blocked", "rolled_back"):
            with (
                patch(f"{_HANDLER_MODULE}.logger"),
                patch(
                    "baldur.settings.chaos.get_chaos_settings",
                ) as mock_settings,
                patch(
                    f"{_HANDLER_MODULE}._get_chaos_handler_counter",
                ) as mock_get_counter,
            ):
                mock_settings.return_value = MagicMock(enabled=True)
                mock_counter = MagicMock()
                mock_get_counter.return_value = mock_counter

                _on_chaos_experiment_stopped(
                    _make_event(
                        EventType.CHAOS_EXPERIMENT_STOPPED,
                        data={"status": known_status},
                    )
                )

                mock_counter.labels.assert_called_with(status=known_status)

    def test_stopped_defaults_to_unknown_when_status_missing(self):
        """Missing status defaults to 'unknown' (not in _KNOWN_STATUSES → stays 'unknown')."""
        with (
            patch(f"{_HANDLER_MODULE}.logger"),
            patch(
                "baldur.settings.chaos.get_chaos_settings",
            ) as mock_settings,
            patch(
                f"{_HANDLER_MODULE}._get_chaos_handler_counter",
            ) as mock_get_counter,
        ):
            mock_settings.return_value = MagicMock(enabled=True)
            mock_counter = MagicMock()
            mock_get_counter.return_value = mock_counter

            _on_chaos_experiment_stopped(
                _make_event(EventType.CHAOS_EXPERIMENT_STOPPED, data={})
            )

            mock_counter.labels.assert_called_with(status="unknown")

    # ---- Edge cases ----

    def test_blocked_handles_none_data_attribute(self):
        """Handler handles event with data=None gracefully."""
        with (
            patch(f"{_HANDLER_MODULE}.logger") as mock_logger,
            patch(
                "baldur.settings.chaos.get_chaos_settings",
            ) as mock_settings,
            patch(f"{_HANDLER_MODULE}._get_chaos_handler_counter"),
        ):
            mock_settings.return_value = MagicMock(enabled=True)
            event = MagicMock()
            event.data = None
            _on_chaos_experiment_blocked(event)
            mock_logger.warning.assert_called_once()

    def test_metrics_counter_singleton_caches(self):
        """Counter singleton is created once and cached."""
        import baldur.services.event_bus.bus._chaos_handlers as mod

        original = mod._chaos_handler_counter
        mod._chaos_handler_counter = None

        try:
            with patch(
                "baldur.metrics.registry.get_or_create_counter",
            ) as mock_create:
                mock_create.return_value = MagicMock()

                first = _get_chaos_handler_counter()
                second = _get_chaos_handler_counter()

                assert first is second
                mock_create.assert_called_once()
        finally:
            mod._chaos_handler_counter = original
