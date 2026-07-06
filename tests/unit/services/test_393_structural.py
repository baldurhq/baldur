"""
Tests for structural fixes (393 §A3, §D).

- A3: event_history_max wire-up from audit settings
- D: __all__ declarations for health_check and system_control
"""

from __future__ import annotations

from unittest.mock import patch

# =============================================================================
# A3. EventBus event_history_max wire-up
# =============================================================================


class TestEventBusMaxHistoryWireUpBehavior:
    """EventBus._max_history reads from audit settings instead of hardcoded 1000."""

    def test_max_history_reads_from_audit_settings(self):
        """EventBus._max_history matches get_audit_settings().event_history_max."""
        from baldur.services.event_bus.bus.event_bus import BaldurEventBus
        from baldur.settings.audit import get_audit_settings

        bus = BaldurEventBus()
        expected = get_audit_settings().event_history_max

        assert bus._max_history == expected

    def test_max_history_fallback_on_settings_error(self):
        """EventBus._max_history falls back to _FALLBACK_MAX_HISTORY when settings fail."""
        from baldur.services.event_bus.bus.event_bus import (
            _FALLBACK_MAX_HISTORY,
            BaldurEventBus,
        )

        with patch(
            "baldur.settings.audit.get_audit_settings",
            autospec=True,
            side_effect=Exception("boom"),
        ):
            result = BaldurEventBus._load_max_history()

        assert result == _FALLBACK_MAX_HISTORY


# =============================================================================
# D. __all__ declarations
# =============================================================================


class TestHealthCheckAllContract:
    """health_check.py __all__ contains all expected public symbols."""

    def test_all_contains_required_symbols(self):
        """__all__ includes all public classes and factory functions."""
        from baldur.services import health_check

        expected = {
            "DatabaseCheck",
            "PoolInfo",
            "SystemHealthSummary",
            "ReadinessStatus",
            "PoolHealthSummary",
            "HealthCheckService",
            "get_health_check_service",
            "configure_health_check_service",
            "reset_health_check_service",
        }

        assert set(health_check.__all__) == expected

    def test_all_symbols_are_importable(self):
        """Every name in __all__ is actually defined in the module."""
        from baldur.services import health_check

        for name in health_check.__all__:
            assert hasattr(health_check, name), (
                f"{name} listed in __all__ but not defined"
            )


class TestSystemControlAllContract:
    """system_control.py __all__ contains all expected public symbols."""

    def test_all_contains_required_symbols(self):
        """__all__ includes all public classes and factory functions."""
        from baldur.services import system_control

        expected = {
            "SystemState",
            "SystemControlManager",
            "get_system_control",
            "configure_system_control",
            "reset_system_control",
            "is_baldur_enabled",
            "is_dry_run",
        }

        assert set(system_control.__all__) == expected

    def test_all_symbols_are_importable(self):
        """Every name in __all__ is actually defined in the module."""
        from baldur.services import system_control

        for name in system_control.__all__:
            assert hasattr(system_control, name), (
                f"{name} listed in __all__ but not defined"
            )
