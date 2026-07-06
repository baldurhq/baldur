"""
DLQ Replay automation settings tests.

ReplayAutomationSettings:
- On-recovery: event-driven auto-replay on CB recovery
- Traffic-aware: replay gated on traffic normalization
- Adaptive initial batch size + per-domain differentiated policy
"""

import pytest
from pydantic import ValidationError


class TestReplayAutomationSettings:
    """Tests for ReplayAutomationSettings."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        """Reset singleton before and after each test."""
        from baldur.settings.replay_automation import (
            reset_replay_automation_settings,
        )

        reset_replay_automation_settings()
        yield
        reset_replay_automation_settings()

    def test_default_values(self):
        """Verify default values."""
        from baldur.settings.replay_automation import ReplayAutomationSettings

        settings = ReplayAutomationSettings()

        # On-recovery defaults
        assert settings.on_recovery_enabled is True
        assert settings.on_recovery_max_items == 100

        # Adaptive initial batch size default
        assert settings.adaptive_initial_items == 50

    def test_env_override(self, monkeypatch):
        """Verify an env var actually binds to the field (non-default value)."""
        from baldur.settings.replay_automation import ReplayAutomationSettings

        # Non-default value (default is 100) so the assertion fails if the
        # env var never binds — guards against a stale/renamed env var.
        monkeypatch.setenv("BALDUR_REPLAY_AUTOMATION_ON_RECOVERY_MAX_ITEMS", "200")

        settings = ReplayAutomationSettings()

        assert settings.on_recovery_max_items == 200

    def test_validation_max_items_range(self):
        """Verify max_items range constraints."""
        from baldur.settings.replay_automation import ReplayAutomationSettings

        with pytest.raises(ValidationError):
            ReplayAutomationSettings(on_recovery_max_items=0)  # < 1

        with pytest.raises(ValidationError):
            ReplayAutomationSettings(adaptive_initial_items=2000)  # > 1000

    def test_singleton_pattern(self):
        """Verify the singleton pattern works."""
        from baldur.settings.replay_automation import (
            get_replay_automation_settings,
        )

        settings1 = get_replay_automation_settings()
        settings2 = get_replay_automation_settings()

        assert settings1 is settings2
