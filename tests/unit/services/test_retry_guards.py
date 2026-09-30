"""
ErrorBudgetGuard unit tests.

Target: services/retry_handler/guards.py
- ErrorBudgetGuard: error-budget gate pre-check (fail-open)

The retry handler exports no kill-switch guard: while the switch is pulled,
Baldur's automatic interventions step aside through the execution-mode
resolver.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

import baldur.services.retry_handler.guards as retry_guards
from baldur.interfaces.resilience_policy import PolicyContext
from baldur.services.retry_handler.guards import ErrorBudgetGuard


class TestKillSwitchGuardRemovedContract:
    """The retry-handler guards module no longer defines a kill-switch guard."""

    def test_no_kill_switch_guard(self):
        assert not hasattr(retry_guards, "KillSwitchGuard")


# =============================================================================
# ErrorBudgetGuard — contract
# =============================================================================


class TestErrorBudgetGuardContract:
    """ErrorBudgetGuard fixed identifier."""

    def test_name_is_error_budget(self):
        """ErrorBudgetGuard.name is 'error_budget'."""
        assert ErrorBudgetGuard().name == "error_budget"


# =============================================================================
# ErrorBudgetGuard — behavior
# =============================================================================


class TestErrorBudgetGuardBehavior:
    """ErrorBudgetGuard behavior, including context passing and fail-open."""

    @pytest.fixture(autouse=True)
    def _require_pro(self):
        pytest.importorskip("baldur_pro")

    @patch("baldur_pro.services.error_budget_gate.check_automation_allowed")
    def test_allowed_when_budget_sufficient(self, mock_gate):
        """A sufficient error budget returns allowed=True."""
        gate_result = MagicMock(allowed=True, error_budget_percent=45.0)
        mock_gate.return_value = gate_result

        result = ErrorBudgetGuard().check()
        assert result.allowed is True
        assert result.metadata["error_budget_percent"] == 45.0

    @patch("baldur_pro.services.error_budget_gate.check_automation_allowed")
    def test_blocked_when_budget_low(self, mock_gate):
        """An insufficient error budget returns allowed=False."""
        gate_result = MagicMock(
            allowed=False, error_budget_percent=5.0, threshold_percent=10.0
        )
        mock_gate.return_value = gate_result

        result = ErrorBudgetGuard().check()
        assert result.allowed is False
        assert "budget" in result.reason.lower()
        assert result.metadata["error_budget_percent"] == 5.0
        assert result.metadata["threshold_percent"] == 10.0

    @patch("baldur_pro.services.error_budget_gate.check_automation_allowed")
    def test_passes_context_tier_id_and_region(self, mock_gate):
        """context.tier_id and region are passed to check_automation_allowed."""
        mock_gate.return_value = MagicMock(allowed=True, error_budget_percent=50.0)
        ctx = PolicyContext(tier_id="critical", region="us-east")
        ErrorBudgetGuard().check(context=ctx)
        mock_gate.assert_called_once_with(tier_id="critical", region="us-east")

    @patch("baldur_pro.services.error_budget_gate.check_automation_allowed")
    def test_context_none_passes_none_values(self, mock_gate):
        """context=None passes tier_id=None and region=None."""
        mock_gate.return_value = MagicMock(allowed=True, error_budget_percent=50.0)
        ErrorBudgetGuard().check(context=None)
        mock_gate.assert_called_once_with(tier_id=None, region=None)

    def test_fail_open_on_import_error(self):
        """A failed check_automation_allowed import fails open."""
        with patch(
            "baldur_pro.services.error_budget_gate.check_automation_allowed",
            side_effect=ImportError("not found"),
        ):
            result = ErrorBudgetGuard().check()
            assert result.allowed is True

    def test_fail_open_on_runtime_error(self):
        """An error while calling check_automation_allowed fails open."""
        with patch(
            "baldur_pro.services.error_budget_gate.check_automation_allowed",
            side_effect=RuntimeError("service unavailable"),
        ):
            result = ErrorBudgetGuard().check()
            assert result.allowed is True
