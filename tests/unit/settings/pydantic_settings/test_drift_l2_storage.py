"""
Tests for DriftThresholdSettings and L2StorageSettings.
"""

import pytest
from pydantic import ValidationError


class TestDriftThresholdSettings:
    """Tests for DriftThresholdSettings."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        """Reset singleton before and after each test."""
        from baldur.settings.drift_threshold import reset_drift_threshold_settings

        reset_drift_threshold_settings()
        yield
        reset_drift_threshold_settings()

    def test_default_values(self):
        """Verify defaults — Dormant tier per V1_LAUNCH_MANIFEST (527 D1)."""
        from baldur.settings.drift_threshold import DriftThresholdSettings

        settings = DriftThresholdSettings()

        assert settings.enabled is False
        assert settings.warning_threshold == 0.05
        assert settings.critical_threshold == 0.20
        assert settings.incident_threshold == 0.50
        assert settings.alert_enabled is False

    def test_env_override(self, monkeypatch):
        """환경변수로 값을 오버라이드할 수 있는지 검증."""
        from baldur.settings.drift_threshold import DriftThresholdSettings

        monkeypatch.setenv("BALDUR_DRIFT_THRESHOLD_WARNING_THRESHOLD", "0.10")

        settings = DriftThresholdSettings()

        assert settings.warning_threshold == 0.10

    def test_validation_threshold_range(self):
        """warning_threshold 범위 (0.01-0.50) 검증."""
        from baldur.settings.drift_threshold import DriftThresholdSettings

        with pytest.raises(ValidationError):
            DriftThresholdSettings(warning_threshold=0.0)

        with pytest.raises(ValidationError):
            DriftThresholdSettings(warning_threshold=0.6)

    def test_singleton_pattern(self):
        """싱글톤 패턴이 동작하는지 검증."""
        from baldur.settings.drift_threshold import get_drift_threshold_settings

        settings1 = get_drift_threshold_settings()
        settings2 = get_drift_threshold_settings()

        assert settings1 is settings2


class TestL2StorageSettings:
    """Tests for L2StorageSettings."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        """Reset singleton before and after each test."""
        from baldur.settings.l2_storage import reset_l2_storage_settings

        reset_l2_storage_settings()
        yield
        reset_l2_storage_settings()

    def test_default_values(self):
        """기본값 검증."""
        from baldur.settings.l2_storage import L2StorageSettings

        settings = L2StorageSettings()

        assert settings.enabled is False  # Disabled by default
        assert settings.redis_timeout_ms == 1000  # 479 D1
        assert settings.reconciliation_interval_seconds == 300

    def test_env_override(self, monkeypatch):
        """환경변수로 값을 오버라이드할 수 있는지 검증."""
        from baldur.settings.l2_storage import L2StorageSettings

        monkeypatch.setenv("BALDUR_L2_STORAGE_REDIS_TIMEOUT_MS", "500")

        settings = L2StorageSettings()

        assert settings.redis_timeout_ms == 500

    def test_validation_redis_timeout_range(self):
        """redis_timeout_ms 범위 (10-1000) 검증."""
        from baldur.settings.l2_storage import L2StorageSettings

        with pytest.raises(ValidationError):
            L2StorageSettings(redis_timeout_ms=5)

        with pytest.raises(ValidationError):
            L2StorageSettings(redis_timeout_ms=1001)

    def test_singleton_pattern(self):
        """싱글톤 패턴이 동작하는지 검증."""
        from baldur.settings.l2_storage import get_l2_storage_settings

        settings1 = get_l2_storage_settings()
        settings2 = get_l2_storage_settings()

        assert settings1 is settings2
