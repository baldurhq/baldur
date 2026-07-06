"""RecoveryShutdownSettings 471 drain field tests.

Coverage (impl 471 D3, D4, D7):
- ``drain_liveness_paths``: default empty list, accepts list[str] override
- ``drain_default_retry_after_seconds``: default 5.0, boundaries [1.0, 300.0]
- ``hooks_check_delay_seconds``: default 2.0, boundaries [0.5, 30.0]
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from baldur.settings.recovery_shutdown import RecoveryShutdownSettings

# =============================================================================
# Contract values from impl 471 D3, D4, D7
# =============================================================================


class TestRecoveryShutdownSettingsDrainFieldsContract:
    """471 D3/D4/D7 design-spec values."""

    def test_drain_liveness_paths_default_is_empty_list(self):
        """471 D3: operator override starts empty — canonical defaults are hardcoded
        in the middleware, not the settings field."""
        settings = RecoveryShutdownSettings()
        assert settings.drain_liveness_paths == []

    def test_drain_default_retry_after_seconds_default_value(self):
        """471 D4 default: 5.0 seconds."""
        settings = RecoveryShutdownSettings()
        assert settings.drain_default_retry_after_seconds == 5.0

    def test_hooks_check_delay_seconds_default_value(self):
        """471 D7 default: 2.0 seconds."""
        settings = RecoveryShutdownSettings()
        assert settings.hooks_check_delay_seconds == 2.0


# =============================================================================
# Boundary analysis — drain_default_retry_after_seconds [1.0, 300.0]
# =============================================================================


class TestDrainDefaultRetryAfterBoundaryContract:
    """471 D4 ge=1.0, le=300.0 boundary verification."""

    def test_minimum_boundary_one_second_accepted(self):
        settings = RecoveryShutdownSettings(drain_default_retry_after_seconds=1.0)
        assert settings.drain_default_retry_after_seconds == 1.0

    def test_below_minimum_raises_validation_error(self):
        with pytest.raises(ValidationError):
            RecoveryShutdownSettings(drain_default_retry_after_seconds=0.99)

    def test_maximum_boundary_three_hundred_accepted(self):
        settings = RecoveryShutdownSettings(drain_default_retry_after_seconds=300.0)
        assert settings.drain_default_retry_after_seconds == 300.0

    def test_above_maximum_raises_validation_error(self):
        with pytest.raises(ValidationError):
            RecoveryShutdownSettings(drain_default_retry_after_seconds=300.1)


# =============================================================================
# Boundary analysis — hooks_check_delay_seconds [0.5, 30.0]
# =============================================================================


class TestHooksCheckDelayBoundaryContract:
    """471 D7 ge=0.5, le=30.0 boundary verification."""

    def test_minimum_boundary_half_second_accepted(self):
        settings = RecoveryShutdownSettings(hooks_check_delay_seconds=0.5)
        assert settings.hooks_check_delay_seconds == 0.5

    def test_below_minimum_raises_validation_error(self):
        with pytest.raises(ValidationError):
            RecoveryShutdownSettings(hooks_check_delay_seconds=0.49)

    def test_maximum_boundary_thirty_seconds_accepted(self):
        settings = RecoveryShutdownSettings(hooks_check_delay_seconds=30.0)
        assert settings.hooks_check_delay_seconds == 30.0

    def test_above_maximum_raises_validation_error(self):
        with pytest.raises(ValidationError):
            RecoveryShutdownSettings(hooks_check_delay_seconds=30.1)


# =============================================================================
# drain_liveness_paths — accepts operator override
# =============================================================================


class TestDrainLivenessPathsBehavior:
    """471 D3: operator override path list."""

    def test_accepts_list_of_strings(self):
        paths = ["/livez", "/healthz/live", "/k8s/alive"]
        settings = RecoveryShutdownSettings(drain_liveness_paths=paths)
        assert settings.drain_liveness_paths == paths

    def test_accepts_empty_list(self):
        settings = RecoveryShutdownSettings(drain_liveness_paths=[])
        assert settings.drain_liveness_paths == []
