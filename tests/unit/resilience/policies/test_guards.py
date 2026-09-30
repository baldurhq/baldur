"""
ErrorBudgetGuard unit tests (#231).

Targets:
- resilience/policies/guards/error_budget.py (ErrorBudgetGuard)
- resilience/policies/guards/__init__.py (re-export)

The kill switch is not a guard: while it is pulled, Baldur's automatic
interventions step aside through the execution-mode resolver, so the guards
package exports no kill-switch guard.

UNIT_TEST_GUIDELINES.md:
- Contract tests: hardcoded expected values (name string, reason string)
- Behavior tests: source-referenced (GuardResult attributes)
- conftest.py placement: a fixture used by one file stays in the file (§5.1)
"""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import MagicMock, patch

from baldur.interfaces.resilience_policy import (
    PolicyContext,
)
from baldur.resilience.policies.guards import (
    ErrorBudgetGuard,
)

# =============================================================================
# Contract — ErrorBudgetGuard
# =============================================================================


class TestErrorBudgetGuardContract:
    """ErrorBudgetGuard contract."""

    def test_name(self):
        """name is 'error_budget_gate'."""
        guard = ErrorBudgetGuard()
        assert guard.name == "error_budget_gate"


# =============================================================================
# Behavior — ErrorBudgetGuard
# =============================================================================


class TestErrorBudgetGuardBehavior:
    """ErrorBudgetGuard behavior."""

    def test_import_error_fail_open(self):
        """A failed ErrorBudgetGate import fails open (allowed=True)."""
        guard = ErrorBudgetGuard()
        with patch.dict(
            "sys.modules",
            {"baldur_pro.services.error_budget_gate.gate": None},
        ):
            result = guard.check()
            assert result.allowed is True

    def test_context_none_global_check(self):
        """context=None decides globally (tier_id=None, region=None)."""
        guard = ErrorBudgetGuard()

        @dataclass
        class MockGateResult:
            allowed: bool = True
            reason: str | None = None
            error_budget_percent: float = 80.0
            threshold_percent: float = 10.0

        mock_module = MagicMock()
        mock_module.check_automation_allowed.return_value = MockGateResult(allowed=True)

        with patch.dict(
            "sys.modules",
            {"baldur_pro.services.error_budget_gate.gate": mock_module},
        ):
            result = guard.check(context=None)

        mock_module.check_automation_allowed.assert_called_once_with(
            tier_id=None, region=None
        )
        assert result.allowed is True

    def test_context_tier_and_region_passed(self):
        """context.tier_id/region are passed to check_automation_allowed."""
        guard = ErrorBudgetGuard()
        ctx = PolicyContext(tier_id="critical", region="us-west-2")

        @dataclass
        class MockGateResult:
            allowed: bool = True
            reason: str | None = None
            error_budget_percent: float = 80.0
            threshold_percent: float = 10.0

        mock_module = MagicMock()
        mock_module.check_automation_allowed.return_value = MockGateResult(allowed=True)

        with patch.dict(
            "sys.modules",
            {"baldur_pro.services.error_budget_gate.gate": mock_module},
        ):
            result = guard.check(context=ctx)

        mock_module.check_automation_allowed.assert_called_once_with(
            tier_id="critical", region="us-west-2"
        )
        assert result.allowed is True

    def test_gate_not_allowed_returns_rejected(self):
        """A gate answering allowed=False makes the GuardResult allowed=False."""
        guard = ErrorBudgetGuard()

        @dataclass
        class MockGateResult:
            allowed: bool = False
            reason: str = "Budget exhausted"
            error_budget_percent: float = 2.0
            threshold_percent: float = 5.0

        mock_module = MagicMock()
        mock_module.check_automation_allowed.return_value = MockGateResult()

        with patch.dict(
            "sys.modules",
            {"baldur_pro.services.error_budget_gate.gate": mock_module},
        ):
            result = guard.check()

        assert result.allowed is False
        assert result.reason == "Budget exhausted"
        assert result.metadata["error_budget_percent"] == 2.0
        assert result.metadata["threshold_percent"] == 5.0

    def test_gate_not_allowed_with_none_reason(self):
        """A gate reason of None falls back to 'Error budget exhausted'."""
        guard = ErrorBudgetGuard()

        @dataclass
        class MockGateResult:
            allowed: bool = False
            reason: str | None = None
            error_budget_percent: float = 0.0
            threshold_percent: float = 5.0

        mock_module = MagicMock()
        mock_module.check_automation_allowed.return_value = MockGateResult()

        with patch.dict(
            "sys.modules",
            {"baldur_pro.services.error_budget_gate.gate": mock_module},
        ):
            result = guard.check()

        assert result.allowed is False
        assert result.reason == "Error budget exhausted"

    def test_exception_fail_open(self):
        """An unexpected exception inside check fails open (allowed=True)."""
        guard = ErrorBudgetGuard()
        mock_module = MagicMock()
        mock_module.check_automation_allowed.side_effect = RuntimeError("gate error")

        with patch.dict(
            "sys.modules",
            {"baldur_pro.services.error_budget_gate.gate": mock_module},
        ):
            result = guard.check()

        assert result.allowed is True


# =============================================================================
# Contract — guards __init__.py re-export
# =============================================================================


class TestGuardsInitReexportContract:
    """guards/__init__.py re-export contract."""

    def test_kill_switch_guard_not_exported(self):
        """No kill-switch guard is exported: the switch rides the resolver."""
        import baldur.resilience.policies.guards as guards

        assert not hasattr(guards, "KillSwitchGuard")
        assert "KillSwitchGuard" not in guards.__all__

    def test_error_budget_guard_exported(self):
        """ErrorBudgetGuard is importable from the guards package."""
        from baldur.resilience.policies.guards import ErrorBudgetGuard

        assert ErrorBudgetGuard is not None
