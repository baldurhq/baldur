"""
Unit tests for DailyReportSettings.shadow_pro_mode (impl 452).

Verification techniques:
- Contract: Literal range, default value, env-var override
- Boundary analysis: each of 4 valid Literal values + 1 invalid rejected
"""

from __future__ import annotations

import pytest


class TestDailyReportSettingsShadowProContract:
    """shadow_pro_mode field contract per 452 D1, D6."""

    def test_default_is_auto(self):
        """shadow_pro_mode defaults to 'auto' per D6."""
        from baldur.settings.daily_report import DailyReportSettings

        settings = DailyReportSettings()
        assert settings.shadow_pro_mode == "auto"

    def test_accepts_auto(self):
        """'auto' is a valid Literal value."""
        from baldur.settings.daily_report import DailyReportSettings

        settings = DailyReportSettings(shadow_pro_mode="auto")
        assert settings.shadow_pro_mode == "auto"

    def test_accepts_daily(self):
        """'daily' is a valid Literal value."""
        from baldur.settings.daily_report import DailyReportSettings

        settings = DailyReportSettings(shadow_pro_mode="daily")
        assert settings.shadow_pro_mode == "daily"

    def test_accepts_weekly(self):
        """'weekly' is a valid Literal value."""
        from baldur.settings.daily_report import DailyReportSettings

        settings = DailyReportSettings(shadow_pro_mode="weekly")
        assert settings.shadow_pro_mode == "weekly"

    def test_accepts_off(self):
        """'off' is a valid Literal value."""
        from baldur.settings.daily_report import DailyReportSettings

        settings = DailyReportSettings(shadow_pro_mode="off")
        assert settings.shadow_pro_mode == "off"

    def test_rejects_unknown_value(self):
        """Any value outside the Literal set raises ValidationError."""
        from pydantic import ValidationError

        from baldur.settings.daily_report import DailyReportSettings

        with pytest.raises(ValidationError):
            DailyReportSettings(shadow_pro_mode="never")

    def test_env_prefix_is_baldur_daily_report(self):
        """env_prefix matches BALDUR_DAILY_REPORT_ per D1."""
        from baldur.settings.daily_report import DailyReportSettings

        assert DailyReportSettings.model_config["env_prefix"] == "BALDUR_DAILY_REPORT_"


class TestDailyReportSettingsShadowProBehavior:
    """Env-var override and singleton lifecycle for shadow_pro_mode."""

    def test_env_var_overrides_default(self, monkeypatch):
        """BALDUR_DAILY_REPORT_SHADOW_PRO_MODE env var overrides default."""
        from baldur.settings.daily_report import DailyReportSettings

        monkeypatch.setenv("BALDUR_DAILY_REPORT_SHADOW_PRO_MODE", "weekly")

        settings = DailyReportSettings()

        assert settings.shadow_pro_mode == "weekly"

    def test_env_var_off_disables_block(self, monkeypatch):
        """BALDUR_DAILY_REPORT_SHADOW_PRO_MODE=off is honored."""
        from baldur.settings.daily_report import DailyReportSettings

        monkeypatch.setenv("BALDUR_DAILY_REPORT_SHADOW_PRO_MODE", "off")

        settings = DailyReportSettings()

        assert settings.shadow_pro_mode == "off"

    def test_env_var_invalid_value_rejected(self, monkeypatch):
        """Invalid env var value raises ValidationError at construction."""
        from pydantic import ValidationError

        from baldur.settings.daily_report import DailyReportSettings

        monkeypatch.setenv("BALDUR_DAILY_REPORT_SHADOW_PRO_MODE", "monthly")

        with pytest.raises(ValidationError):
            DailyReportSettings()
