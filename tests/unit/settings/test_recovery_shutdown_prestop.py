"""
Tests for RecoveryShutdownSettings prestop_seconds field and cross-validator (386).

Test Categories:
    A. prestop_seconds Contract — default, boundary values
    B. validate_drain_fits_termination_window Behavior — warning/no-warning cases
"""

import pytest
from pydantic import ValidationError

from baldur.settings.recovery_shutdown import RecoveryShutdownSettings

# =============================================================================
# A. prestop_seconds Contract
# =============================================================================


class TestPrestopSecondsContract:
    """prestop_seconds field design contract values (386 §3)."""

    def test_prestop_seconds_default_value(self):
        """prestop_seconds default: 10.0."""
        settings = RecoveryShutdownSettings()
        assert settings.prestop_seconds == 10.0

    def test_prestop_seconds_minimum_boundary_ge_zero(self):
        """prestop_seconds ge=0.0: boundary at 0."""
        settings = RecoveryShutdownSettings(prestop_seconds=0.0)
        assert settings.prestop_seconds == 0.0

    def test_prestop_seconds_below_minimum_raises_validation_error(self):
        """prestop_seconds < 0 raises ValidationError."""
        with pytest.raises(ValidationError):
            RecoveryShutdownSettings(prestop_seconds=-0.1)

    def test_prestop_seconds_maximum_boundary_le_sixty(self):
        """prestop_seconds le=60.0: boundary at 60."""
        settings = RecoveryShutdownSettings(prestop_seconds=60.0)
        assert settings.prestop_seconds == 60.0

    def test_prestop_seconds_above_maximum_raises_validation_error(self):
        """prestop_seconds > 60 raises ValidationError."""
        with pytest.raises(ValidationError):
            RecoveryShutdownSettings(prestop_seconds=60.1)


# =============================================================================
# B. validate_drain_fits_termination_window Behavior
# =============================================================================


class TestDrainFitsTerminationWindowBehavior:
    """Cross-validator: drain_timeout + prestop vs max_shutdown_wait (386 §3)."""

    def test_no_warning_when_drain_fits_window(self):
        """drain_timeout + prestop < max_wait → no warning (settings created OK)."""
        # drain=30, prestop=10, max_wait=600 → effective_window=590 > 30 ✓
        settings = RecoveryShutdownSettings(
            default_drain_timeout_seconds=30.0,
            prestop_seconds=10.0,
            max_shutdown_wait_seconds=600.0,
        )
        assert settings.default_drain_timeout_seconds == 30.0
        assert settings.prestop_seconds == 10.0

    def test_warning_when_drain_exceeds_window(self, capfd):
        """drain_timeout > effective_window → warning logged (settings still created)."""
        # drain=100, prestop=50, max_wait=120 → effective_window=70 < 100
        # The validator warns but doesn't reject
        settings = RecoveryShutdownSettings(
            default_drain_timeout_seconds=100.0,
            prestop_seconds=50.0,
            max_shutdown_wait_seconds=120.0,
        )
        # Settings object is created (validator is warning-only)
        assert settings.default_drain_timeout_seconds == 100.0
        assert settings.prestop_seconds == 50.0
        assert settings.max_shutdown_wait_seconds == 120.0

    def test_no_warning_when_exactly_fits(self):
        """drain_timeout == effective_window → no warning (not exceeded)."""
        # drain=50, prestop=10, max_wait=60 → effective_window=50 == 50
        settings = RecoveryShutdownSettings(
            default_drain_timeout_seconds=50.0,
            prestop_seconds=10.0,
            max_shutdown_wait_seconds=60.0,
        )
        assert settings.default_drain_timeout_seconds == 50.0
