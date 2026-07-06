"""
Tests for NotificationSettings and ErrorBudgetSettings.
"""

import pytest
from pydantic import ValidationError


class TestNotificationSettings:
    """Tests for NotificationSettings."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        """Reset singleton before and after each test."""
        from baldur.settings.notification import reset_notification_settings

        reset_notification_settings()
        yield
        reset_notification_settings()

    def test_default_values(self):
        """기본값이 core/config.py:NotificationConfig와 일치하는지 검증."""
        from baldur.settings.notification import NotificationSettings

        settings = NotificationSettings()

        assert settings.enabled is True
        assert settings.critical_threshold == 10
        assert settings.warning_threshold == 5
        assert settings.description_max_length == 500

    def test_env_override(self, monkeypatch):
        """환경변수로 값을 오버라이드할 수 있는지 검증."""
        from baldur.settings.notification import NotificationSettings

        monkeypatch.setenv("BALDUR_NOTIFICATION_CRITICAL_THRESHOLD", "20")

        settings = NotificationSettings()

        assert settings.critical_threshold == 20

    def test_validation_threshold_range(self):
        """critical_threshold 범위 (1-100) 검증."""
        from baldur.settings.notification import NotificationSettings

        with pytest.raises(ValidationError):
            NotificationSettings(critical_threshold=0)

        with pytest.raises(ValidationError):
            NotificationSettings(critical_threshold=101)

    def test_singleton_pattern(self):
        """싱글톤 패턴이 동작하는지 검증."""
        from baldur.settings.notification import get_notification_settings

        settings1 = get_notification_settings()
        settings2 = get_notification_settings()

        assert settings1 is settings2


class TestErrorBudgetSettings:
    """Tests for ErrorBudgetSettings."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        """Reset singleton before and after each test."""
        from baldur.settings.error_budget import reset_error_budget_settings

        reset_error_budget_settings()
        yield
        reset_error_budget_settings()

    def test_default_values(self):
        """기본값이 core/config.py:ErrorBudgetConfig와 일치하는지 검증.

        heartbeat_enabled 기본값은 False (impl 527, v1.1 deferred).
        """
        from baldur.settings.error_budget import ErrorBudgetSettings

        settings = ErrorBudgetSettings()

        assert settings.threshold_healthy == 75.0
        assert settings.threshold_caution == 50.0
        assert settings.threshold_warning == 20.0
        assert settings.threshold_critical == 0.0
        assert settings.burn_rate_fast_critical == 14.4
        assert settings.heartbeat_enabled is False
        assert settings.heartbeat_interval_seconds == 60

    def test_env_override(self, monkeypatch):
        """환경변수로 값을 오버라이드할 수 있는지 검증."""
        from baldur.settings.error_budget import ErrorBudgetSettings

        monkeypatch.setenv("BALDUR_ERROR_BUDGET_BURN_RATE_FAST_CRITICAL", "20.0")

        settings = ErrorBudgetSettings()

        assert settings.burn_rate_fast_critical == 20.0

    def test_validation_threshold_range(self):
        """threshold_healthy 범위 (50.0-100.0) 검증."""
        from baldur.settings.error_budget import ErrorBudgetSettings

        with pytest.raises(ValidationError):
            ErrorBudgetSettings(threshold_healthy=40.0)

        with pytest.raises(ValidationError):
            ErrorBudgetSettings(threshold_healthy=101.0)

    def test_singleton_pattern(self):
        """싱글톤 패턴이 동작하는지 검증."""
        from baldur.settings.error_budget import get_error_budget_settings

        settings1 = get_error_budget_settings()
        settings2 = get_error_budget_settings()

        assert settings1 is settings2
