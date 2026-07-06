"""ThrottleGovernanceGuard unit tests.

Target: ``baldur.resilience.policies.guards.governance.ThrottleGovernanceGuard``

Scope:
- ``name`` contract value
- ``check()`` ordering: Break Glass → Kill Switch → Emergency → Error Budget
- Break Glass active short-circuits the other checks
- Each ``_check_*`` method fails open on exceptions raised by the
  ``GovernanceChecker`` provider
- 516 D3 — provider is resolved via ``ProviderRegistry.governance``
  (single cached lookup; NoOp default keeps the guard fail-open when PRO
  is absent)
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from structlog.testing import capture_logs

from baldur.factory.registry import ProviderRegistry
from baldur.resilience.policies.guards.governance import (
    ThrottleGovernanceGuard,
)


def _emitted_warning(cap_logs: list[dict], event_name: str) -> bool:
    """Check structlog event was emitted at WARNING level."""
    return any(
        e.get("event") == event_name and e.get("log_level") == "warning"
        for e in cap_logs
    )


def _make_checker(
    is_system_enabled: bool = True,
    emergency_blocking: tuple[bool, str] = (False, "NONE"),
    error_budget_blocking: tuple[bool, float, float] = (False, 100.0, 0.0),
) -> MagicMock:
    """Build a GovernanceChecker mock with configurable per-method returns."""
    checker = MagicMock()
    checker.is_system_enabled.return_value = is_system_enabled
    checker.is_emergency_blocking.return_value = emergency_blocking
    checker.is_error_budget_blocking.return_value = error_budget_blocking
    return checker


# =============================================================================
# Name contract
# =============================================================================


class TestThrottleGovernanceGuardNameContract:
    """ThrottleGovernanceGuard.name contract."""

    def test_name_is_throttle_governance(self):
        guard = ThrottleGovernanceGuard()
        assert guard.name == "throttle_governance"


# =============================================================================
# check() pass behavior
# =============================================================================


class TestThrottleGovernanceGuardPassBehavior:
    """All checks pass returns allowed."""

    def test_all_checks_pass_returns_allowed(self):
        guard = ThrottleGovernanceGuard()
        checker = _make_checker()

        with (
            patch.object(guard, "_is_break_glass_active", return_value=False),
            ProviderRegistry.governance.override(checker),
        ):
            result = guard.check()
            assert result.allowed is True


# =============================================================================
# Break Glass bypass
# =============================================================================


class TestThrottleGovernanceGuardBreakGlassBehavior:
    """Break Glass active bypasses subsequent checks."""

    def test_break_glass_active_bypasses_all_checks(self):
        guard = ThrottleGovernanceGuard()
        checker = _make_checker(is_system_enabled=False)  # would otherwise reject

        with (
            patch.object(guard, "_is_break_glass_active", return_value=True),
            ProviderRegistry.governance.override(checker),
        ):
            result = guard.check()
            assert result.allowed is True
            checker.is_system_enabled.assert_not_called()
            checker.is_emergency_blocking.assert_not_called()
            checker.is_error_budget_blocking.assert_not_called()


# =============================================================================
# Kill Switch rejection
# =============================================================================


class TestThrottleGovernanceGuardKillSwitchBehavior:
    """Kill Switch rejection."""

    def test_kill_switch_disabled_rejects(self):
        guard = ThrottleGovernanceGuard()
        checker = _make_checker(is_system_enabled=False)

        with (
            patch.object(guard, "_is_break_glass_active", return_value=False),
            ProviderRegistry.governance.override(checker),
        ):
            result = guard.check()
            assert result.allowed is False
            assert result.reason == "kill_switch_disabled"

    def test_kill_switch_exception_failopen(self):
        """``is_system_enabled`` raising falls through to allowed (fail-open)."""
        guard = ThrottleGovernanceGuard()
        checker = _make_checker()
        checker.is_system_enabled.side_effect = RuntimeError("boom")

        with (
            patch.object(guard, "_is_break_glass_active", return_value=False),
            ProviderRegistry.governance.override(checker),
        ):
            result = guard.check()
            assert result.allowed is True


# =============================================================================
# Emergency Level rejection
# =============================================================================


class TestThrottleGovernanceGuardEmergencyBehavior:
    """Emergency Level rejection."""

    def test_emergency_level_3_rejects(self):
        guard = ThrottleGovernanceGuard()
        checker = _make_checker(emergency_blocking=(True, "LEVEL_3"))

        with (
            patch.object(guard, "_is_break_glass_active", return_value=False),
            ProviderRegistry.governance.override(checker),
        ):
            result = guard.check()
            assert result.allowed is False
            assert "emergency_level_LEVEL_3" in result.reason

    def test_emergency_level_under_threshold_passes(self):
        guard = ThrottleGovernanceGuard()
        checker = _make_checker(emergency_blocking=(False, "LEVEL_2"))

        with (
            patch.object(guard, "_is_break_glass_active", return_value=False),
            ProviderRegistry.governance.override(checker),
        ):
            result = guard.check()
            assert result.allowed is True

    def test_emergency_exception_failopen(self):
        guard = ThrottleGovernanceGuard()
        checker = _make_checker()
        checker.is_emergency_blocking.side_effect = RuntimeError("boom")

        with (
            patch.object(guard, "_is_break_glass_active", return_value=False),
            ProviderRegistry.governance.override(checker),
        ):
            result = guard.check()
            assert result.allowed is True


# =============================================================================
# Error Budget rejection
# =============================================================================


class TestThrottleGovernanceGuardErrorBudgetBehavior:
    """Error Budget rejection."""

    def test_error_budget_exhausted_rejects(self):
        guard = ThrottleGovernanceGuard()
        checker = _make_checker(error_budget_blocking=(True, 0.0, 5.0))

        with (
            patch.object(guard, "_is_break_glass_active", return_value=False),
            ProviderRegistry.governance.override(checker),
        ):
            result = guard.check()
            assert result.allowed is False
            assert "error_budget" in result.reason

    def test_error_budget_exception_failopen(self):
        guard = ThrottleGovernanceGuard()
        checker = _make_checker()
        checker.is_error_budget_blocking.side_effect = RuntimeError("boom")

        with (
            patch.object(guard, "_is_break_glass_active", return_value=False),
            ProviderRegistry.governance.override(checker),
        ):
            result = guard.check()
            assert result.allowed is True


# =============================================================================
# Break Glass fail-open behavior (settings import)
# =============================================================================


class TestThrottleGovernanceGuardBreakGlassFailOpenBehavior:
    """Break Glass module failure falls back to "not active"."""

    def test_break_glass_import_error_returns_false(self):
        guard = ThrottleGovernanceGuard()

        with patch.dict("sys.modules", {"baldur.settings.governance": None}):
            assert guard._is_break_glass_active() is False

    def test_break_glass_exception_failopen_warns(self):
        guard = ThrottleGovernanceGuard()

        mock_settings_module = MagicMock()
        mock_settings_module.get_governance_settings.side_effect = RuntimeError(
            "settings read failed"
        )

        with (
            patch.dict(
                "sys.modules", {"baldur.settings.governance": mock_settings_module}
            ),
            capture_logs() as cap_logs,
        ):
            assert guard._is_break_glass_active() is False

        assert _emitted_warning(cap_logs, "guard.check_failed_fail_open")
