"""
ThrottleSettings extension field tests.

Test targets:
1. CB integration settings fields (cb_open_limit_percent, cb_half_open_limit_percent)
2. Recovery Dampening settings fields
"""


class TestThrottleSettingsCBFields:
    """CB integration settings field tests."""

    def test_default_cb_limit_percents(self):
        """Verify default CB limit ratios."""
        from baldur.settings.throttle import (
            ThrottleSettings,
            reset_throttle_settings,
        )

        reset_throttle_settings()

        settings = ThrottleSettings()

        assert settings.cb_open_limit_percent == 0.0
        assert settings.cb_half_open_limit_percent == 0.5

    def test_cb_limit_from_env(self, monkeypatch):
        """Test CB limit ratio configuration via env vars."""
        from baldur.settings.throttle import (
            ThrottleSettings,
            reset_throttle_settings,
        )

        reset_throttle_settings()

        monkeypatch.setenv("BALDUR_THROTTLE_CB_OPEN_LIMIT_PERCENT", "0.1")
        monkeypatch.setenv("BALDUR_THROTTLE_CB_HALF_OPEN_LIMIT_PERCENT", "0.6")

        settings = ThrottleSettings()

        assert settings.cb_open_limit_percent == 0.1
        assert settings.cb_half_open_limit_percent == 0.6


class TestThrottleSettingsRecoveryDampeningFields:
    """Recovery Dampening settings field tests."""

    def test_default_recovery_dampening_settings(self):
        """Verify default Recovery Dampening settings."""
        from baldur.settings.throttle import (
            ThrottleSettings,
            reset_throttle_settings,
        )

        reset_throttle_settings()

        settings = ThrottleSettings()

        assert settings.recovery_dampening_enabled is True
        assert settings.recovery_step_1_percent == 0.8
        assert settings.recovery_step_2_percent == 0.9
        assert settings.recovery_step_3_percent == 1.0
        assert settings.recovery_step_interval_seconds == 30.0

    def test_recovery_steps_from_env(self, monkeypatch):
        """Test Recovery step configuration via env vars."""
        from baldur.settings.throttle import (
            ThrottleSettings,
            reset_throttle_settings,
        )

        reset_throttle_settings()

        monkeypatch.setenv("BALDUR_THROTTLE_RECOVERY_STEP_1_PERCENT", "0.6")
        monkeypatch.setenv("BALDUR_THROTTLE_RECOVERY_STEP_INTERVAL_SECONDS", "60.0")

        settings = ThrottleSettings()

        assert settings.recovery_step_1_percent == 0.6
        assert settings.recovery_step_interval_seconds == 60.0
