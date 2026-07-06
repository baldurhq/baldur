"""
Safety Event Handler Pipeline Integration Tests (376).

Verifies multi-component interactions for safety-critical event handlers.

Test Categories:
    A. Security Violation Emergency Pipeline:
        - Full pipeline: security event → emergency activation → cache invalidation
        - Failure isolation: activate_auto failure stops pipeline before cache
    B. Error Budget Three-Status Pipeline:
        - Full lifecycle: WARNING → CRITICAL → RECOVERED with audit/cache/metric
        - Unified counter: all 3 statuses share single Prometheus counter
    C. Event Bus Dispatch Pipeline:
        - Handler registration: all new handlers discoverable via subscriptions

Note: All tests use mock-based dependencies - no infra dependency.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from baldur.services.event_bus import (
    BaldurEvent,
    EventType,
)
from baldur.services.event_bus.bus.default_handlers import (
    _on_error_budget_critical,
    _on_error_budget_recovered,
    _on_error_budget_warning,
    _on_security_violation_critical,
)


def _make_event(event_type: EventType, data: dict, source: str = "test") -> BaldurEvent:
    return BaldurEvent(event_type=event_type, data=data, source=source)


class TestSecurityViolationEmergencyPipeline:
    """Integration: security violation → emergency mode → governance cache."""

    @pytest.fixture(autouse=True)
    def _require_pro(self):
        pytest.importorskip("baldur_pro")

    @patch("baldur.services.event_bus.bus.default_handlers.logger")
    def test_full_pipeline_activation_and_cache_invalidation(self, mock_logger):
        """
        Purpose: Verify complete pipeline from security event to emergency activation
        and governance cache invalidation.

        Expected: activate_auto called with LEVEL_2, cache invalidated, success logged.
        """
        from baldur_pro.services.emergency_mode.enums import EmergencyLevel

        with (
            patch(
                "baldur_pro.services.emergency_mode.get_emergency_manager",
            ) as mock_get_mgr,
            patch(
                "baldur_pro.services.governance.checks.invalidate_governance_cache",
            ) as mock_cache,
        ):
            # Given: emergency manager configured
            mock_manager = MagicMock()
            mock_get_mgr.return_value = mock_manager

            event = _make_event(
                EventType.SECURITY_VIOLATION_CRITICAL,
                data={
                    "violation_type": "injection_attempt",
                    "severity": "critical",
                    "incident_id": 777,
                    "source_ip": "192.168.1.100",
                },
            )

            # When
            _on_security_violation_critical(event)

            # Then: emergency activation
            mock_manager.activate_auto.assert_called_once_with(
                level=EmergencyLevel.LEVEL_2,
                reason="Security violation: injection_attempt (incident #777)",
            )
            # Then: governance cache invalidated
            mock_cache.assert_called_once()
            # Then: success logged
            success_calls = [
                c
                for c in mock_logger.warning.call_args_list
                if c.args[0] == "event_bus.security_violation_emergency_activated"
            ]
            assert len(success_calls) == 1
            assert success_calls[0].kwargs["violation_type"] == "injection_attempt"
            assert success_calls[0].kwargs["incident_id"] == 777

    @patch("baldur.services.event_bus.bus.default_handlers.logger")
    def test_emergency_failure_stops_pipeline(self, mock_logger):
        """
        Purpose: When activate_auto fails, pipeline stops before cache invalidation.

        Expected: error logged, no cache invalidation attempted, no success log.
        """
        with (
            patch(
                "baldur_pro.services.emergency_mode.get_emergency_manager",
            ) as mock_get_mgr,
            patch(
                "baldur_pro.services.governance.checks.invalidate_governance_cache",
            ) as mock_cache,
        ):
            mock_manager = MagicMock()
            mock_manager.activate_auto.side_effect = ConnectionError("redis down")
            mock_get_mgr.return_value = mock_manager

            event = _make_event(
                EventType.SECURITY_VIOLATION_CRITICAL,
                data={"violation_type": "token_forged", "incident_id": 1},
            )

            # When
            _on_security_violation_critical(event)

            # Then: failure logged with traceback (handler uses logger.exception)
            mock_logger.exception.assert_called_once()
            # Then: cache NOT invalidated (early return)
            mock_cache.assert_not_called()


class TestErrorBudgetThreeStatusPipeline:
    """Integration: error budget 3-piece set (WARNING/CRITICAL/RECOVERED) pipeline."""

    @pytest.fixture(autouse=True)
    def _require_pro(self):
        pytest.importorskip("baldur_pro")

    @patch(
        "baldur.services.event_bus.bus.default_handlers._get_error_budget_event_counter",
    )
    def test_warning_critical_recovered_lifecycle(self, mock_counter_fn):
        """
        Purpose: Verify full lifecycle WARNING → CRITICAL → RECOVERED triggers
        all handlers with correct audit/cache/metric calls.

        Expected: Each handler calls cache, audit, and metric with correct status label.
        """
        mock_counter = MagicMock()
        mock_counter_fn.return_value = mock_counter

        with (
            patch(
                "baldur_pro.services.governance.checks.invalidate_governance_cache",
            ) as mock_cache,
            patch(
                "baldur.services.event_bus.bus.default_handlers.log_error_budget_warning_audit",
            ) as mock_warning_audit,
            patch(
                "baldur.services.event_bus.bus.default_handlers.log_error_budget_blocked_audit",
            ) as mock_blocked_audit,
            patch(
                "baldur.services.event_bus.bus.default_handlers.log_error_budget_recovered_audit",
            ) as mock_recovered_audit,
            patch(
                "baldur.services.event_bus.bus.default_handlers.logger",
            ),
        ):
            # Step 1: WARNING event
            warning_event = _make_event(
                EventType.ERROR_BUDGET_WARNING,
                data={"budget_percent": 18.0, "threshold": 20.0},
            )
            _on_error_budget_warning(warning_event)

            assert mock_cache.call_count == 1
            mock_warning_audit.assert_called_once_with(18.0, 20.0)
            mock_counter.labels.assert_any_call(status="warning")

            # Step 2: CRITICAL event
            critical_event = _make_event(
                EventType.ERROR_BUDGET_CRITICAL,
                data={"budget_percent": 5.0, "threshold": 10.0},
            )
            _on_error_budget_critical(critical_event)

            assert mock_cache.call_count == 2
            mock_blocked_audit.assert_called_once_with(
                action="error_budget_critical_event",
                gate_status="CRITICAL",
                error_budget_percent=5.0,
                threshold_percent=10.0,
            )
            mock_counter.labels.assert_any_call(status="critical")

            # Step 3: RECOVERED event
            recovered_event = _make_event(
                EventType.ERROR_BUDGET_RECOVERED,
                data={"budget_percent": 25.0, "threshold": 20.0},
            )
            _on_error_budget_recovered(recovered_event)

            assert mock_cache.call_count == 3
            mock_recovered_audit.assert_called_once_with(25.0, 20.0)
            mock_counter.labels.assert_any_call(status="recovered")

    @patch(
        "baldur.services.event_bus.bus.default_handlers._get_error_budget_event_counter",
    )
    def test_all_three_statuses_share_single_counter(self, mock_counter_fn):
        """
        Purpose: D3 — all 3 statuses use the same unified counter with status labels.

        Expected: Same counter object used for all three .labels(status=...) calls.
        """
        mock_counter = MagicMock()
        mock_counter_fn.return_value = mock_counter

        with (
            patch(
                "baldur_pro.services.governance.checks.invalidate_governance_cache",
            ),
            patch(
                "baldur.services.event_bus.bus.default_handlers.log_error_budget_warning_audit",
            ),
            patch(
                "baldur.services.event_bus.bus.default_handlers.log_error_budget_blocked_audit",
            ),
            patch(
                "baldur.services.event_bus.bus.default_handlers.log_error_budget_recovered_audit",
            ),
            patch(
                "baldur.services.event_bus.bus.default_handlers.logger",
            ),
        ):
            _on_error_budget_warning(
                _make_event(
                    EventType.ERROR_BUDGET_WARNING,
                    data={"budget_percent": 18.0, "threshold": 20.0},
                )
            )
            _on_error_budget_critical(
                _make_event(
                    EventType.ERROR_BUDGET_CRITICAL,
                    data={"budget_percent": 5.0, "threshold": 10.0},
                )
            )
            _on_error_budget_recovered(
                _make_event(
                    EventType.ERROR_BUDGET_RECOVERED,
                    data={"budget_percent": 25.0, "threshold": 20.0},
                )
            )

            # All three used same counter (3 .labels calls)
            status_labels = [
                call.kwargs.get("status") or call.args[0]
                for call in mock_counter.labels.call_args_list
            ]
            assert "warning" in status_labels
            assert "critical" in status_labels
            assert "recovered" in status_labels


class TestEventBusDispatchPipeline:
    """Integration: event bus subscription → handler dispatch."""

    def setup_method(self):
        from baldur.services.event_bus import get_event_bus

        self.bus = get_event_bus()
        self.bus.reset()

    def teardown_method(self):
        self.bus.reset()

    def test_all_new_handlers_registered_after_default_registration(self):
        """
        Purpose: Verify that all 376 handlers are registered and
        discoverable via event bus subscriptions.

        Expected: All 3 new event types have named handlers subscribed.
        """
        from baldur.services.event_bus.bus.default_handlers import (
            register_default_handlers,
        )

        register_default_handlers()

        expected = {
            EventType.SECURITY_VIOLATION_CRITICAL: "_on_security_violation_critical",
            EventType.ERROR_BUDGET_WARNING: "_on_error_budget_warning",
            EventType.ERROR_BUDGET_RECOVERED: "_on_error_budget_recovered",
        }

        for event_type, handler_name in expected.items():
            subs = self.bus.get_subscriptions(event_type)
            names = [s["handler_name"] for s in subs]
            assert handler_name in names, (
                f"{handler_name} not found in {event_type} subscriptions: {names}"
            )
