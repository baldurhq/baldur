"""
Safety Event Handler Tests (376).

Tests for:
1. _on_security_violation_critical — Emergency Mode LEVEL_2 activation
2. _on_error_budget_warning — WARNING transition handler
3. _on_error_budget_recovered — RECOVERED transition handler
4. _on_error_budget_critical — Upgraded CRITICAL handler
5. _get_error_budget_event_counter — Lazy singleton metrics counter
6. register_default_handlers — New handler registration
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from baldur.services.event_bus import BaldurEvent, EventType
from baldur.services.event_bus.bus.default_handlers import (
    _on_emergency_level_changed,
    _on_error_budget_critical,
    _on_error_budget_recovered,
    _on_error_budget_warning,
    _on_security_violation_critical,
)
from baldur.services.event_bus.bus.event_types import EventPriority


def _make_event(event_type: EventType, data: dict, source: str = "test") -> BaldurEvent:
    """Create a BaldurEvent for testing."""
    return BaldurEvent(event_type=event_type, data=data, source=source)


# =============================================================================
# Contract Tests — Verify design-doc specified values (376 Decision Log)
# =============================================================================


class TestSafetyEventHandlerContract:
    """Contract verification for 376 safety event handlers."""

    def test_critical_handler_threshold_fallback_is_10(self):
        """D11: CRITICAL handler threshold fallback must be 10 (not 20)."""
        with patch(
            "baldur.services.event_bus.bus.default_handlers.logger",
        ) as mock_logger:
            event = _make_event(EventType.ERROR_BUDGET_CRITICAL, data={})
            _on_error_budget_critical(event)

            mock_logger.warning.assert_any_call(
                "event_bus.error_budget_critical_handled",
                budget_percent=0,
                threshold=10,
            )

    def test_warning_handler_threshold_fallback_is_20(self):
        """WARNING handler threshold fallback must be 20 (warning_threshold_percent default)."""
        with patch(
            "baldur.services.event_bus.bus.default_handlers.logger",
        ) as mock_logger:
            event = _make_event(EventType.ERROR_BUDGET_WARNING, data={})
            _on_error_budget_warning(event)

            mock_logger.warning.assert_any_call(
                "event_bus.error_budget_warning_received",
                budget_percent=0,
                threshold=20,
            )

    def test_recovered_handler_threshold_fallback_is_20(self):
        """RECOVERED handler threshold fallback must be 20."""
        with patch(
            "baldur.services.event_bus.bus.default_handlers.logger",
        ) as mock_logger:
            event = _make_event(EventType.ERROR_BUDGET_RECOVERED, data={})
            _on_error_budget_recovered(event)

            mock_logger.info.assert_any_call(
                "event_bus.error_budget_recovered",
                budget_percent=0,
                threshold=20,
            )

    def test_security_handler_activates_level_2(self):
        """D1/Handler 1: Security violation triggers LEVEL_2 emergency."""
        pytest.importorskip("baldur_pro")
        from baldur_pro.services.emergency_mode.enums import EmergencyLevel

        with (
            patch(
                "baldur_pro.services.emergency_mode.get_emergency_manager",
            ) as mock_get_mgr,
            patch(
                "baldur.services.event_bus.bus.default_handlers.logger",
            ),
        ):
            mock_manager = MagicMock()
            mock_get_mgr.return_value = mock_manager

            event = _make_event(
                EventType.SECURITY_VIOLATION_CRITICAL,
                data={"violation_type": "token_forged", "incident_id": 42},
            )
            _on_security_violation_critical(event)

            mock_manager.activate_auto.assert_called_once_with(
                level=EmergencyLevel.LEVEL_2,
                reason="Security violation: token_forged (incident #42)",
            )

    def test_critical_handler_event_name_is_event_bus_prefix(self):
        """D10: CRITICAL handler uses event_bus.* prefix, not event_handler.*."""
        with patch(
            "baldur.services.event_bus.bus.default_handlers.logger",
        ) as mock_logger:
            event = _make_event(
                EventType.ERROR_BUDGET_CRITICAL,
                data={"budget_percent": 5.0, "threshold": 10.0},
            )
            _on_error_budget_critical(event)

            event_names = [call.args[0] for call in mock_logger.warning.call_args_list]
            assert "event_bus.error_budget_critical_handled" in event_names
            assert "event_handler.error_budget_critical_threshold" not in event_names

    def test_metrics_counter_name_and_labels(self):
        """D3/D4: Counter name and status label from design doc."""
        import baldur.services.event_bus.bus.default_handlers as mod

        # Reset singleton to force re-creation
        mod._error_budget_event_counter = None

        with patch(
            "baldur.metrics.registry.get_or_create_counter",
        ) as mock_create:
            mock_counter = MagicMock()
            mock_create.return_value = mock_counter

            result = mod._get_error_budget_event_counter()

            mock_create.assert_called_once_with(
                "baldur_error_budget_event_handled_total",
                "Total error budget events handled by default handlers",
                ["status"],
            )
            assert result is mock_counter

        # Cleanup
        mod._error_budget_event_counter = None

    def test_handler_registration_priorities(self):
        """Contract: handler registration uses correct priorities per doc."""
        from baldur.services.event_bus import get_event_bus
        from baldur.services.event_bus.bus.default_handlers import (
            register_default_handlers,
        )

        bus = get_event_bus()
        bus.reset()

        try:
            register_default_handlers()

            # Check SECURITY_VIOLATION_CRITICAL → CRITICAL priority
            security_subs = bus.get_subscriptions(EventType.SECURITY_VIOLATION_CRITICAL)
            security_handler = next(
                s
                for s in security_subs
                if s["handler_name"] == "_on_security_violation_critical"
            )
            assert security_handler["priority"] == EventPriority.CRITICAL.name

            # Check ERROR_BUDGET_WARNING → HIGH priority
            warning_subs = bus.get_subscriptions(EventType.ERROR_BUDGET_WARNING)
            warning_handler = next(
                s
                for s in warning_subs
                if s["handler_name"] == "_on_error_budget_warning"
            )
            assert warning_handler["priority"] == EventPriority.HIGH.name

            # Check ERROR_BUDGET_RECOVERED → NORMAL priority
            recovered_subs = bus.get_subscriptions(EventType.ERROR_BUDGET_RECOVERED)
            recovered_handler = next(
                s
                for s in recovered_subs
                if s["handler_name"] == "_on_error_budget_recovered"
            )
            assert recovered_handler["priority"] == EventPriority.NORMAL.name
        finally:
            bus.reset()


# =============================================================================
# Behavior Tests — Verify handler logic and side effects
# =============================================================================


class TestSecurityViolationCriticalBehavior:
    """Behavior tests for _on_security_violation_critical handler."""

    @pytest.fixture(autouse=True)
    def _require_pro(self):
        pytest.importorskip("baldur_pro")

    def _make_security_event(self, **overrides):
        data = {
            "violation_type": "data_tampered",
            "severity": "critical",
            "incident_id": 100,
            "source_ip": "10.0.0.1",
            "user_id": 5,
            "trigger_source": "security_violation_service",
        }
        data.update(overrides)
        return _make_event(
            EventType.SECURITY_VIOLATION_CRITICAL,
            data=data,
            source="security_service",
        )

    @patch(
        "baldur.services.event_bus.bus.default_handlers.logger",
    )
    def test_activate_auto_failure_returns_early(self, mock_logger):
        """activate_auto() exception → error log + early return (no cache invalidation)."""
        with patch(
            "baldur_pro.services.emergency_mode.get_emergency_manager",
        ) as mock_get_mgr:
            mock_manager = MagicMock()
            mock_manager.activate_auto.side_effect = RuntimeError("db down")
            mock_get_mgr.return_value = mock_manager

            event = self._make_security_event()
            _on_security_violation_critical(event)

            # Error logged via .exception() for automatic traceback capture
            mock_logger.exception.assert_called_once()
            assert (
                "event_bus.security_violation_emergency_failed"
                in mock_logger.exception.call_args.args
            )

            # Success log NOT called (early return)
            success_calls = [
                c
                for c in mock_logger.warning.call_args_list
                if c.args[0] == "event_bus.security_violation_emergency_activated"
            ]
            assert len(success_calls) == 0

    @patch(
        "baldur.services.event_bus.bus.default_handlers.logger",
    )
    def test_cache_invalidation_failure_does_not_block_success_log(self, mock_logger):
        """Governance cache failure is non-critical — success log still emitted."""
        with (
            patch(
                "baldur_pro.services.emergency_mode.get_emergency_manager",
            ) as mock_get_mgr,
            patch(
                "baldur_pro.services.governance.checks.invalidate_governance_cache",
                side_effect=RuntimeError("cache error"),
            ),
        ):
            mock_manager = MagicMock()
            mock_get_mgr.return_value = mock_manager

            event = self._make_security_event()
            _on_security_violation_critical(event)

            # Cache failure warning logged
            cache_fail_calls = [
                c
                for c in mock_logger.warning.call_args_list
                if c.args[0] == "event_bus.governance_cache_invalidation_failed"
            ]
            assert len(cache_fail_calls) == 1

            # Success log still emitted
            success_calls = [
                c
                for c in mock_logger.warning.call_args_list
                if c.args[0] == "event_bus.security_violation_emergency_activated"
            ]
            assert len(success_calls) == 1

    @patch(
        "baldur.services.event_bus.bus.default_handlers.logger",
    )
    def test_missing_event_data_uses_defaults(self, mock_logger):
        """Missing violation_type/incident_id defaults to 'unknown'."""
        with patch(
            "baldur_pro.services.emergency_mode.get_emergency_manager",
        ) as mock_get_mgr:
            mock_manager = MagicMock()
            mock_get_mgr.return_value = mock_manager

            event = _make_event(EventType.SECURITY_VIOLATION_CRITICAL, data={})
            _on_security_violation_critical(event)

            mock_manager.activate_auto.assert_called_once()
            call_kwargs = mock_manager.activate_auto.call_args.kwargs
            assert (
                call_kwargs["reason"]
                == "Security violation: unknown (incident #unknown)"
            )

    @patch(
        "baldur.services.event_bus.bus.default_handlers.logger",
    )
    def test_get_emergency_manager_failure_returns_early(self, mock_logger):
        """get_emergency_manager() init failure → error log + early return."""
        with (
            patch(
                "baldur_pro.services.emergency_mode.get_emergency_manager",
                side_effect=RuntimeError("singleton init failed"),
            ),
            patch(
                "baldur_pro.services.governance.checks.invalidate_governance_cache",
            ) as mock_cache,
        ):
            event = self._make_security_event()
            _on_security_violation_critical(event)

            # Error logged via .exception() for automatic traceback capture
            mock_logger.exception.assert_called_once()
            assert (
                "event_bus.security_violation_emergency_failed"
                in mock_logger.exception.call_args.args
            )

            # Early return — cache NOT invalidated, success log NOT emitted
            mock_cache.assert_not_called()
            success_calls = [
                c
                for c in mock_logger.warning.call_args_list
                if c.args[0] == "event_bus.security_violation_emergency_activated"
            ]
            assert len(success_calls) == 0


class TestErrorBudgetWarningBehavior:
    """Behavior tests for _on_error_budget_warning handler."""

    @pytest.fixture(autouse=True)
    def _require_pro(self):
        pytest.importorskip("baldur_pro")

    def _make_warning_event(self, budget_percent=15.0, threshold=20.0):
        return _make_event(
            EventType.ERROR_BUDGET_WARNING,
            data={"budget_percent": budget_percent, "threshold": threshold},
        )

    @patch(
        "baldur.services.event_bus.bus.default_handlers._get_error_budget_event_counter",
    )
    @patch(
        "baldur.services.event_bus.bus.default_handlers.log_error_budget_warning_audit",
    )
    @patch(
        "baldur_pro.services.governance.checks.invalidate_governance_cache",
    )
    @patch(
        "baldur.services.event_bus.bus.default_handlers.logger",
    )
    def test_all_side_effects_called(
        self, mock_logger, mock_cache, mock_audit, mock_counter_fn
    ):
        """Handler calls: log → cache → audit → metric."""
        mock_counter = MagicMock()
        mock_counter_fn.return_value = mock_counter

        event = self._make_warning_event(budget_percent=18.5, threshold=20.0)
        _on_error_budget_warning(event)

        # 1. Structured log
        mock_logger.warning.assert_any_call(
            "event_bus.error_budget_warning_received",
            budget_percent=18.5,
            threshold=20.0,
        )
        # 2. Cache invalidation
        mock_cache.assert_called_once()
        # 3. Audit
        mock_audit.assert_called_once_with(18.5, 20.0)
        # 4. Metric
        mock_counter.labels.assert_called_once_with(status="warning")
        mock_counter.labels.return_value.inc.assert_called_once()

    @patch(
        "baldur.services.event_bus.bus.default_handlers._get_error_budget_event_counter",
    )
    @patch(
        "baldur.services.event_bus.bus.default_handlers.logger",
    )
    def test_cache_failure_isolated_audit_still_called(
        self, mock_logger, mock_counter_fn
    ):
        """Cache invalidation failure does not prevent audit or metric."""
        mock_counter = MagicMock()
        mock_counter_fn.return_value = mock_counter

        with (
            patch(
                "baldur_pro.services.governance.checks.invalidate_governance_cache",
                side_effect=RuntimeError("cache down"),
            ),
            patch(
                "baldur.services.event_bus.bus.default_handlers.log_error_budget_warning_audit",
            ) as mock_audit,
        ):
            event = self._make_warning_event()
            _on_error_budget_warning(event)

            # Cache failure logged
            mock_logger.warning.assert_any_call(
                "event_bus.governance_cache_invalidation_failed"
            )
            # Audit still called
            mock_audit.assert_called_once()
            # Metric still called
            mock_counter.labels.assert_called_once_with(status="warning")

    @patch(
        "baldur.services.event_bus.bus.default_handlers._get_error_budget_event_counter",
    )
    @patch(
        "baldur.services.event_bus.bus.default_handlers.logger",
    )
    def test_audit_failure_isolated_metric_still_called(
        self, mock_logger, mock_counter_fn
    ):
        """Audit failure does not prevent metric increment.

        Post-518-a: the audit helper (baldur.audit.helpers) is fail-open
        natively — PRO failures are swallowed inside _safe_delegate and the
        wrapper returns None. The handler no longer wraps the call in
        try/except. This test simulates that contract by patching the helper
        to return None (the fail-open result), and verifies the metric still
        increments downstream of the no-op audit call.
        """
        mock_counter = MagicMock()
        mock_counter_fn.return_value = mock_counter

        with (
            patch(
                "baldur_pro.services.governance.checks.invalidate_governance_cache",
            ),
            patch(
                "baldur.services.event_bus.bus.default_handlers.log_error_budget_warning_audit",
                return_value=None,
            ),
        ):
            event = self._make_warning_event()
            _on_error_budget_warning(event)

            mock_counter.labels.assert_called_once_with(status="warning")

    @patch(
        "baldur.services.event_bus.bus.default_handlers._get_error_budget_event_counter",
    )
    @patch(
        "baldur.services.event_bus.bus.default_handlers.logger",
    )
    def test_metric_failure_logs_debug(self, mock_logger, mock_counter_fn):
        """Metric increment failure logs DEBUG, does not propagate."""
        mock_counter = MagicMock()
        mock_counter.labels.return_value.inc.side_effect = RuntimeError("prometheus")
        mock_counter_fn.return_value = mock_counter

        with (
            patch(
                "baldur_pro.services.governance.checks.invalidate_governance_cache",
            ),
            patch(
                "baldur.services.event_bus.bus.default_handlers.log_error_budget_warning_audit",
            ),
        ):
            event = self._make_warning_event()
            _on_error_budget_warning(event)

            mock_logger.debug.assert_any_call(
                "event_bus.error_budget_metric_increment_failed",
                status="warning",
            )


class TestErrorBudgetRecoveredBehavior:
    """Behavior tests for _on_error_budget_recovered handler."""

    @pytest.fixture(autouse=True)
    def _require_pro(self):
        pytest.importorskip("baldur_pro")

    def _make_recovered_event(self, budget_percent=25.0, threshold=20.0):
        return _make_event(
            EventType.ERROR_BUDGET_RECOVERED,
            data={"budget_percent": budget_percent, "threshold": threshold},
        )

    @patch(
        "baldur.services.event_bus.bus.default_handlers._get_error_budget_event_counter",
    )
    @patch(
        "baldur.services.event_bus.bus.default_handlers.log_error_budget_recovered_audit",
    )
    @patch(
        "baldur_pro.services.governance.checks.invalidate_governance_cache",
    )
    @patch(
        "baldur.services.event_bus.bus.default_handlers.logger",
    )
    def test_all_side_effects_called(
        self, mock_logger, mock_cache, mock_audit, mock_counter_fn
    ):
        """Handler calls: log → cache → audit → metric."""
        mock_counter = MagicMock()
        mock_counter_fn.return_value = mock_counter

        event = self._make_recovered_event(budget_percent=25.0, threshold=20.0)
        _on_error_budget_recovered(event)

        # 1. Structured log at INFO level
        mock_logger.info.assert_any_call(
            "event_bus.error_budget_recovered",
            budget_percent=25.0,
            threshold=20.0,
        )
        # 2. Cache invalidation
        mock_cache.assert_called_once()
        # 3. Audit
        mock_audit.assert_called_once_with(25.0, 20.0)
        # 4. Metric
        mock_counter.labels.assert_called_once_with(status="recovered")
        mock_counter.labels.return_value.inc.assert_called_once()

    @patch(
        "baldur.services.event_bus.bus.default_handlers._get_error_budget_event_counter",
    )
    @patch(
        "baldur.services.event_bus.bus.default_handlers.logger",
    )
    def test_cache_failure_isolated(self, mock_logger, mock_counter_fn):
        """Cache failure does not prevent audit or metric."""
        mock_counter = MagicMock()
        mock_counter_fn.return_value = mock_counter

        with (
            patch(
                "baldur_pro.services.governance.checks.invalidate_governance_cache",
                side_effect=RuntimeError("cache down"),
            ),
            patch(
                "baldur.services.event_bus.bus.default_handlers.log_error_budget_recovered_audit",
            ) as mock_audit,
        ):
            event = self._make_recovered_event()
            _on_error_budget_recovered(event)

            mock_logger.warning.assert_any_call(
                "event_bus.governance_cache_invalidation_failed"
            )
            mock_audit.assert_called_once()
            mock_counter.labels.assert_called_once_with(status="recovered")

    @patch(
        "baldur.services.event_bus.bus.default_handlers._get_error_budget_event_counter",
    )
    @patch(
        "baldur.services.event_bus.bus.default_handlers.logger",
    )
    def test_audit_failure_isolated_metric_still_called(
        self, mock_logger, mock_counter_fn
    ):
        """Audit failure does not prevent metric increment.

        Post-518-a: helper fail-open replaces the handler's try/except.
        See TestErrorBudgetWarningBehavior version for the full rationale.
        """
        mock_counter = MagicMock()
        mock_counter_fn.return_value = mock_counter

        with (
            patch(
                "baldur_pro.services.governance.checks.invalidate_governance_cache",
            ),
            patch(
                "baldur.services.event_bus.bus.default_handlers.log_error_budget_recovered_audit",
                return_value=None,
            ),
        ):
            event = self._make_recovered_event()
            _on_error_budget_recovered(event)

            mock_counter.labels.assert_called_once_with(status="recovered")

    @patch(
        "baldur.services.event_bus.bus.default_handlers._get_error_budget_event_counter",
    )
    @patch(
        "baldur.services.event_bus.bus.default_handlers.logger",
    )
    def test_metric_failure_logs_debug(self, mock_logger, mock_counter_fn):
        """Metric increment failure logs DEBUG, does not propagate."""
        mock_counter = MagicMock()
        mock_counter.labels.return_value.inc.side_effect = RuntimeError("prometheus")
        mock_counter_fn.return_value = mock_counter

        with (
            patch(
                "baldur_pro.services.governance.checks.invalidate_governance_cache",
            ),
            patch(
                "baldur.services.event_bus.bus.default_handlers.log_error_budget_recovered_audit",
            ),
        ):
            event = self._make_recovered_event()
            _on_error_budget_recovered(event)

            mock_logger.debug.assert_any_call(
                "event_bus.error_budget_metric_increment_failed",
                status="recovered",
            )


class TestErrorBudgetCriticalUpgradeBehavior:
    """Behavior tests for upgraded _on_error_budget_critical handler."""

    @pytest.fixture(autouse=True)
    def _require_pro(self):
        pytest.importorskip("baldur_pro")

    def _make_critical_event(self, budget_percent=5.0, threshold=10.0):
        return _make_event(
            EventType.ERROR_BUDGET_CRITICAL,
            data={"budget_percent": budget_percent, "threshold": threshold},
        )

    @patch(
        "baldur.services.event_bus.bus.default_handlers._get_error_budget_event_counter",
    )
    @patch(
        "baldur.services.event_bus.bus.default_handlers.log_error_budget_blocked_audit",
    )
    @patch(
        "baldur_pro.services.governance.checks.invalidate_governance_cache",
    )
    @patch(
        "baldur.services.event_bus.bus.default_handlers.logger",
    )
    def test_all_side_effects_called(
        self, mock_logger, mock_cache, mock_audit, mock_counter_fn
    ):
        """Upgraded handler calls: log → cache → audit → metric."""
        mock_counter = MagicMock()
        mock_counter_fn.return_value = mock_counter

        event = self._make_critical_event(budget_percent=5.0, threshold=10.0)
        _on_error_budget_critical(event)

        # 1. Structured log
        mock_logger.warning.assert_any_call(
            "event_bus.error_budget_critical_handled",
            budget_percent=5.0,
            threshold=10.0,
        )
        # 2. Cache invalidation
        mock_cache.assert_called_once()
        # 3. Audit with correct params
        mock_audit.assert_called_once_with(
            action="error_budget_critical_event",
            gate_status="CRITICAL",
            error_budget_percent=5.0,
            threshold_percent=10.0,
        )
        # 4. Metric
        mock_counter.labels.assert_called_once_with(status="critical")
        mock_counter.labels.return_value.inc.assert_called_once()

    @patch(
        "baldur.services.event_bus.bus.default_handlers._get_error_budget_event_counter",
    )
    @patch(
        "baldur.services.event_bus.bus.default_handlers.logger",
    )
    def test_cache_failure_isolated_audit_still_called(
        self, mock_logger, mock_counter_fn
    ):
        """Cache failure does not prevent audit or metric."""
        mock_counter = MagicMock()
        mock_counter_fn.return_value = mock_counter

        with (
            patch(
                "baldur_pro.services.governance.checks.invalidate_governance_cache",
                side_effect=RuntimeError("cache down"),
            ),
            patch(
                "baldur.services.event_bus.bus.default_handlers.log_error_budget_blocked_audit",
            ) as mock_audit,
        ):
            event = self._make_critical_event()
            _on_error_budget_critical(event)

            mock_logger.warning.assert_any_call(
                "event_bus.governance_cache_invalidation_failed"
            )
            mock_audit.assert_called_once()
            mock_counter.labels.assert_called_once_with(status="critical")

    @patch(
        "baldur.services.event_bus.bus.default_handlers._get_error_budget_event_counter",
    )
    @patch(
        "baldur.services.event_bus.bus.default_handlers.logger",
    )
    def test_audit_failure_isolated(self, mock_logger, mock_counter_fn):
        """Audit failure does not prevent metric increment.

        Post-518-a: helper fail-open replaces the handler's try/except.
        See TestErrorBudgetWarningBehavior version for the full rationale.
        """
        mock_counter = MagicMock()
        mock_counter_fn.return_value = mock_counter

        with (
            patch(
                "baldur_pro.services.governance.checks.invalidate_governance_cache",
            ),
            patch(
                "baldur.services.event_bus.bus.default_handlers.log_error_budget_blocked_audit",
                return_value=None,
            ),
        ):
            event = self._make_critical_event()
            _on_error_budget_critical(event)

            mock_counter.labels.assert_called_once_with(status="critical")

    @patch(
        "baldur.services.event_bus.bus.default_handlers._get_error_budget_event_counter",
    )
    @patch(
        "baldur.services.event_bus.bus.default_handlers.logger",
    )
    def test_metric_failure_logs_debug(self, mock_logger, mock_counter_fn):
        """Metric increment failure logs DEBUG, does not propagate."""
        mock_counter = MagicMock()
        mock_counter.labels.return_value.inc.side_effect = RuntimeError("prometheus")
        mock_counter_fn.return_value = mock_counter

        with (
            patch(
                "baldur_pro.services.governance.checks.invalidate_governance_cache",
            ),
            patch(
                "baldur.services.event_bus.bus.default_handlers.log_error_budget_blocked_audit",
            ),
        ):
            event = self._make_critical_event()
            _on_error_budget_critical(event)

            mock_logger.debug.assert_any_call(
                "event_bus.error_budget_metric_increment_failed",
                status="critical",
            )


class TestErrorBudgetEventCounterSingleton:
    """Behavior tests for _get_error_budget_event_counter lazy singleton."""

    def test_singleton_caches_counter(self):
        """Second call returns same object without calling get_or_create_counter."""
        import baldur.services.event_bus.bus.default_handlers as mod

        mod._error_budget_event_counter = None

        with patch(
            "baldur.metrics.registry.get_or_create_counter",
        ) as mock_create:
            mock_counter = MagicMock()
            mock_create.return_value = mock_counter

            # Given: first call creates counter
            result1 = mod._get_error_budget_event_counter()
            assert mock_create.call_count == 1

            # When: second call
            result2 = mod._get_error_budget_event_counter()

            # Then: same object, no second creation
            assert result1 is result2
            assert mock_create.call_count == 1

        # Cleanup
        mod._error_budget_event_counter = None


class TestEmergencyLevelChangedBehavior:
    """Behavior tests for upgraded _on_emergency_level_changed handler."""

    def setup_method(self):
        import baldur.services.event_bus.bus.default_handlers as mod

        mod._emergency_level_event_counter = None

    def teardown_method(self):
        import baldur.services.event_bus.bus.default_handlers as mod

        mod._emergency_level_event_counter = None

    def test_escalation_to_level_2_dispatches_high_priority_notification(self):
        """LEVEL_1 → LEVEL_2 escalation sends HIGH priority operator notification."""
        import sys
        import types

        unified_mod = types.ModuleType("baldur_pro.services.unified_notification")
        unified_mod.NotificationCategory = MagicMock()
        unified_mod.NotificationPayload = MagicMock()
        priority_mock = MagicMock()
        priority_mock.HIGH = "HIGH"
        priority_mock.CRITICAL = "CRITICAL"
        unified_mod.NotificationPriority = priority_mock
        manager_class = MagicMock()
        manager_instance = MagicMock()
        manager_instance.notify.return_value = MagicMock(success=True, suppressed=False)
        manager_class.return_value = manager_instance
        unified_mod.UnifiedNotificationManager = manager_class
        unified_mod.get_unified_notification_manager = MagicMock(
            return_value=manager_instance
        )

        with patch.dict(
            sys.modules,
            {"baldur_pro.services.unified_notification": unified_mod},
        ):
            event = _make_event(
                EventType.EMERGENCY_LEVEL_CHANGED,
                data={"level": 2, "previous_level": 1, "reason": "spike"},
            )
            _on_emergency_level_changed(event)

        manager_instance.notify.assert_called_once()
        payload = unified_mod.NotificationPayload.call_args.kwargs
        assert payload["priority"] == "HIGH"
        assert "LEVEL_2" in payload["title"]
        assert payload["dedup_key"] == "emergency_level_escalation:1->2"

    def test_escalation_to_level_3_dispatches_critical_priority(self):
        """LEVEL_2 → LEVEL_3 escalation sends CRITICAL priority operator notification."""
        import sys
        import types

        unified_mod = types.ModuleType("baldur_pro.services.unified_notification")
        unified_mod.NotificationCategory = MagicMock()
        unified_mod.NotificationPayload = MagicMock()
        priority_mock = MagicMock()
        priority_mock.HIGH = "HIGH"
        priority_mock.CRITICAL = "CRITICAL"
        unified_mod.NotificationPriority = priority_mock
        manager_instance = MagicMock()
        manager_instance.notify.return_value = MagicMock(success=True, suppressed=False)
        unified_mod.UnifiedNotificationManager = MagicMock(
            return_value=manager_instance
        )
        unified_mod.get_unified_notification_manager = MagicMock(
            return_value=manager_instance
        )

        with patch.dict(
            sys.modules,
            {"baldur_pro.services.unified_notification": unified_mod},
        ):
            event = _make_event(
                EventType.EMERGENCY_LEVEL_CHANGED,
                data={"level": 3, "previous_level": 2, "reason": "DB outage"},
            )
            _on_emergency_level_changed(event)

        payload = unified_mod.NotificationPayload.call_args.kwargs
        assert payload["priority"] == "CRITICAL"

    def test_de_escalation_skips_notification(self):
        """LEVEL_2 → LEVEL_1 de-escalation does NOT dispatch the firing
        (escalation) notification. The resolve/stand-down path is exercised
        separately by the de-escalation gate matrix."""
        with (
            patch(
                "baldur.services.event_bus.bus.default_handlers."
                "_notify_emergency_level_escalation"
            ) as mock_notify,
            patch(
                "baldur.services.event_bus.bus.default_handlers."
                "_notify_emergency_level_resolved"
            ),
        ):
            event = _make_event(
                EventType.EMERGENCY_LEVEL_CHANGED,
                data={"level": 1, "previous_level": 2, "reason": "recovered"},
            )
            _on_emergency_level_changed(event)

        mock_notify.assert_not_called()

    def test_level_1_escalation_skips_notification(self):
        """NORMAL → LEVEL_1 escalation does NOT dispatch notification (threshold is 2)."""
        with patch(
            "baldur.services.event_bus.bus.default_handlers."
            "_notify_emergency_level_escalation"
        ) as mock_notify:
            event = _make_event(
                EventType.EMERGENCY_LEVEL_CHANGED,
                data={"level": 1, "previous_level": 0, "reason": "minor"},
            )
            _on_emergency_level_changed(event)

        mock_notify.assert_not_called()

    def test_unified_notification_unavailable_fails_open(self):
        """ImportError on baldur_pro returns silently — handler does not crash."""
        import sys

        # Remove module if cached so import inside handler raises
        sys.modules.pop("baldur_pro.services.unified_notification", None)

        with patch.dict(
            sys.modules,
            {"baldur_pro.services.unified_notification": None},
        ):
            event = _make_event(
                EventType.EMERGENCY_LEVEL_CHANGED,
                data={"level": 2, "previous_level": 1, "reason": "test"},
            )
            # Should not raise
            _on_emergency_level_changed(event)

    def test_metric_counter_incremented_on_each_call(self):
        """Prometheus counter labeled with level value increments per event."""
        import baldur.services.event_bus.bus.default_handlers as mod

        with patch(
            "baldur.metrics.registry.get_or_create_counter",
        ) as mock_create:
            mock_counter = MagicMock()
            labeled = MagicMock()
            mock_counter.labels.return_value = labeled
            mock_create.return_value = mock_counter

            event = _make_event(
                EventType.EMERGENCY_LEVEL_CHANGED,
                data={"level": 2, "previous_level": 1, "reason": "x"},
            )
            with patch.object(mod, "_notify_emergency_level_escalation"):
                _on_emergency_level_changed(event)

            mock_counter.labels.assert_called_with(level="2")
            labeled.inc.assert_called_once()


class TestHandlerRegistrationBehavior:
    """Behavior tests for register_default_handlers new subscriptions."""

    def setup_method(self):
        from baldur.services.event_bus import get_event_bus

        self.bus = get_event_bus()
        self.bus.reset()

    def teardown_method(self):
        self.bus.reset()

    def test_security_violation_critical_handler_registered(self):
        """SECURITY_VIOLATION_CRITICAL has _on_security_violation_critical handler."""
        from baldur.services.event_bus.bus.default_handlers import (
            register_default_handlers,
        )

        register_default_handlers()

        subs = self.bus.get_subscriptions(EventType.SECURITY_VIOLATION_CRITICAL)
        handler_names = [s["handler_name"] for s in subs]
        assert "_on_security_violation_critical" in handler_names

    def test_error_budget_warning_handler_registered(self):
        """ERROR_BUDGET_WARNING has _on_error_budget_warning handler."""
        from baldur.services.event_bus.bus.default_handlers import (
            register_default_handlers,
        )

        register_default_handlers()

        subs = self.bus.get_subscriptions(EventType.ERROR_BUDGET_WARNING)
        handler_names = [s["handler_name"] for s in subs]
        assert "_on_error_budget_warning" in handler_names

    def test_error_budget_recovered_handler_registered(self):
        """ERROR_BUDGET_RECOVERED has _on_error_budget_recovered handler."""
        from baldur.services.event_bus.bus.default_handlers import (
            register_default_handlers,
        )

        register_default_handlers()

        subs = self.bus.get_subscriptions(EventType.ERROR_BUDGET_RECOVERED)
        handler_names = [s["handler_name"] for s in subs]
        assert "_on_error_budget_recovered" in handler_names


# =============================================================================
# Emergency de-escalation (resolve / stand-down) — 612 D3
# =============================================================================


def _make_unified_stub():
    """Build a stub baldur_pro.services.unified_notification module.

    Mirrors the stub-module pattern already used for the escalation tests but
    exposes the LOW priority the resolve path needs and the singleton getter
    (612 D7) the resolve path calls.
    """
    import types

    unified_mod = types.ModuleType("baldur_pro.services.unified_notification")
    unified_mod.NotificationCategory = MagicMock()
    unified_mod.NotificationPayload = MagicMock()
    priority_mock = MagicMock()
    priority_mock.LOW = "LOW"
    priority_mock.HIGH = "HIGH"
    priority_mock.CRITICAL = "CRITICAL"
    unified_mod.NotificationPriority = priority_mock
    manager_instance = MagicMock()
    manager_instance.notify.return_value = MagicMock(
        success=True, suppressed=False, suppression_reason=None
    )
    unified_mod.get_unified_notification_manager = MagicMock(
        return_value=manager_instance
    )
    return unified_mod, manager_instance


class TestEmergencyDeEscalationGateBehavior:
    """Behavior tests for the de-escalation (resolve) gate in
    _on_emergency_level_changed (612 D3).

    Notify iff ``previous_severity >= 2 and level_severity < 2`` — the exact
    inverse of the firing gate. Escalations are routed by the existing firing
    path, never by the resolve branch.
    """

    def setup_method(self):
        import baldur.services.event_bus.bus.default_handlers as mod

        mod._emergency_level_event_counter = None

    def teardown_method(self):
        import baldur.services.event_bus.bus.default_handlers as mod

        mod._emergency_level_event_counter = None

    @pytest.mark.parametrize(
        ("prev", "new", "expect_resolve"),
        [
            (3, 2, False),
            (2, 1, True),
            (1, 0, False),
            (3, 0, True),
            (2, 0, True),
        ],
        ids=[
            "3to2_silent",
            "2to1_resolve",
            "1to0_silent",
            "3to0_resolve",
            "2to0_resolve",
        ],
    )
    def test_de_escalation_gate_notifies_only_below_level_2(
        self, prev, new, expect_resolve
    ):
        """Resolve fires only when severity crosses from >=2 down below 2."""
        with (
            patch(
                "baldur.services.event_bus.bus.default_handlers."
                "_notify_emergency_level_resolved"
            ) as mock_resolve,
            patch(
                "baldur.services.event_bus.bus.default_handlers."
                "_notify_emergency_level_escalation"
            ) as mock_escalate,
        ):
            event = _make_event(
                EventType.EMERGENCY_LEVEL_CHANGED,
                data={"level": new, "previous_level": prev, "reason": "recovered"},
            )
            _on_emergency_level_changed(event)

        # De-escalations never take the firing (escalation) path.
        mock_escalate.assert_not_called()
        if expect_resolve:
            mock_resolve.assert_called_once()
        else:
            mock_resolve.assert_not_called()

    def test_full_gradual_recovery_sequence_yields_single_resolve(self):
        """A full L3->NORMAL gradual recovery (3->2, 2->1, 1->0) resolves once.

        The per-step emits of _gradual_recovery_worker (612 D6) drive these
        three de-escalation events. Only the 2->1 crossing trips the gate, so
        the operator receives exactly one stand-down — no mid-incident noise,
        no orphan resolve on the final 1->0 step.
        """
        sequence = [(3, 2), (2, 1), (1, 0)]

        with (
            patch(
                "baldur.services.event_bus.bus.default_handlers."
                "_notify_emergency_level_resolved"
            ) as mock_resolve,
            patch(
                "baldur.services.event_bus.bus.default_handlers."
                "_notify_emergency_level_escalation"
            ) as mock_escalate,
        ):
            for prev, new in sequence:
                _on_emergency_level_changed(
                    _make_event(
                        EventType.EMERGENCY_LEVEL_CHANGED,
                        data={
                            "level": new,
                            "previous_level": prev,
                            "reason": "gradual_recovery",
                        },
                    )
                )

        assert mock_resolve.call_count == 1
        mock_escalate.assert_not_called()


class TestEmergencyResolveNotificationBehavior:
    """Behavior tests for _notify_emergency_level_resolved payload (612 D3).

    Driven end-to-end through _on_emergency_level_changed so the gate and the
    payload contract are verified together. baldur_pro is stubbed at the
    module level, exposing the singleton getter (612 D7).
    """

    def setup_method(self):
        import baldur.services.event_bus.bus.default_handlers as mod

        mod._emergency_level_event_counter = None

    def teardown_method(self):
        import baldur.services.event_bus.bus.default_handlers as mod

        mod._emergency_level_event_counter = None

    def test_resolve_payload_is_low_priority_slack_only_with_dedup_key(self):
        """Resolve payload pins LOW + channels=['slack'] + OPERATIONS + dedup_key."""
        import sys

        unified_mod, manager_instance = _make_unified_stub()

        with patch.dict(
            sys.modules,
            {"baldur_pro.services.unified_notification": unified_mod},
        ):
            event = _make_event(
                EventType.EMERGENCY_LEVEL_CHANGED,
                data={"level": 1, "previous_level": 2, "reason": "recovered"},
            )
            _on_emergency_level_changed(event)

        manager_instance.notify.assert_called_once()
        payload = unified_mod.NotificationPayload.call_args.kwargs
        assert payload["priority"] == "LOW"
        assert payload["channels"] == ["slack"]
        assert payload["category"] is unified_mod.NotificationCategory.OPERATIONS
        assert payload["source"] == "EventHandler.EmergencyLevel"
        assert payload["dedup_key"] == "emergency_level_resolved:2->1"
        assert "LEVEL_1" in payload["title"]

    def test_resolve_uses_singleton_manager_getter(self):
        """The resolve path resolves the manager via the singleton getter (612 D7)."""
        import sys

        unified_mod, _ = _make_unified_stub()

        with patch.dict(
            sys.modules,
            {"baldur_pro.services.unified_notification": unified_mod},
        ):
            event = _make_event(
                EventType.EMERGENCY_LEVEL_CHANGED,
                data={"level": 1, "previous_level": 2, "reason": "recovered"},
            )
            _on_emergency_level_changed(event)

        unified_mod.get_unified_notification_manager.assert_called_once()

    def test_resolve_to_normal_labels_title_normal(self):
        """A crossing to severity 0 labels the stand-down target as NORMAL."""
        import sys

        unified_mod, _ = _make_unified_stub()

        with patch.dict(
            sys.modules,
            {"baldur_pro.services.unified_notification": unified_mod},
        ):
            event = _make_event(
                EventType.EMERGENCY_LEVEL_CHANGED,
                data={"level": 0, "previous_level": 2, "reason": "recovered"},
            )
            _on_emergency_level_changed(event)

        payload = unified_mod.NotificationPayload.call_args.kwargs
        assert "NORMAL" in payload["title"]
        assert payload["dedup_key"] == "emergency_level_resolved:2->0"

    def test_resolve_pro_unavailable_skips_silently(self):
        """ImportError on baldur_pro returns silently — handler does not crash."""
        import sys

        sys.modules.pop("baldur_pro.services.unified_notification", None)

        with patch.dict(
            sys.modules,
            {"baldur_pro.services.unified_notification": None},
        ):
            event = _make_event(
                EventType.EMERGENCY_LEVEL_CHANGED,
                data={"level": 1, "previous_level": 2, "reason": "recovered"},
            )
            # Should not raise
            _on_emergency_level_changed(event)
