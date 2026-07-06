"""
Tests for SLASettings.
"""

import pytest
from pydantic import ValidationError


class TestSLASettings:
    """Tests for SLASettings."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        """Reset singleton before and after each test."""
        from baldur.settings.sla import reset_sla_settings

        reset_sla_settings()
        yield
        reset_sla_settings()

    def test_default_values(self):
        """기본값이 core/config.py:SLAConfig와 일치하는지 검증."""
        from baldur.settings.sla import SLASettings

        settings = SLASettings()

        assert settings.default_hours == 24
        assert settings.thresholds_by_domain == {}

    def test_env_override(self, monkeypatch):
        """환경변수로 값을 오버라이드할 수 있는지 검증."""
        from baldur.settings.sla import SLASettings

        monkeypatch.setenv("BALDUR_SLA_DEFAULT_HOURS", "48")

        settings = SLASettings()

        assert settings.default_hours == 48

    def test_validation_default_hours_range(self):
        """default_hours 범위 (1-720) 검증."""
        from baldur.settings.sla import SLASettings

        with pytest.raises(ValidationError):
            SLASettings(default_hours=0)

        with pytest.raises(ValidationError):
            SLASettings(default_hours=721)

    def test_get_threshold(self):
        """도메인별 임계값 조회 검증."""
        from datetime import timedelta

        from baldur.settings.sla import SLASettings

        settings = SLASettings(
            default_hours=24, thresholds_by_domain={"payment": 1, "order": 2}
        )

        assert settings.get_threshold("payment") == timedelta(hours=1)
        assert settings.get_threshold("order") == timedelta(hours=2)
        assert settings.get_threshold("unknown") == timedelta(hours=24)

    def test_singleton_pattern(self):
        """싱글톤 패턴이 동작하는지 검증."""
        from baldur.settings.sla import get_sla_settings

        settings1 = get_sla_settings()
        settings2 = get_sla_settings()

        assert settings1 is settings2


class TestSLOSettings:
    """Tests for SLOSettings."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        """Reset singleton before and after each test."""
        from baldur.settings.slo import reset_slo_settings

        reset_slo_settings()
        yield
        reset_slo_settings()

    def test_default_values(self):
        """기본값이 core/config.py:SLOConfigRuntime과 일치하는지 검증."""
        from baldur.settings.slo import SLOSettings

        settings = SLOSettings()

        assert settings.default_window_days == 30
        assert settings.default_target == 0.999
        assert settings.default_fast_burn_rate == 14.4
        assert settings.default_slow_burn_rate == 3.0
        assert settings.slos == []

    def test_env_override(self, monkeypatch):
        """환경변수로 값을 오버라이드할 수 있는지 검증."""
        from baldur.settings.slo import SLOSettings

        monkeypatch.setenv("BALDUR_SLO_DEFAULT_WINDOW_DAYS", "7")
        monkeypatch.setenv("BALDUR_SLO_DEFAULT_TARGET", "0.995")

        settings = SLOSettings()

        assert settings.default_window_days == 7
        assert settings.default_target == 0.995

    def test_validation_target_range(self):
        """default_target 범위 (0.9-1.0) 검증."""
        from baldur.settings.slo import SLOSettings

        with pytest.raises(ValidationError):
            SLOSettings(default_target=0.89)

        with pytest.raises(ValidationError):
            SLOSettings(default_target=1.01)

    def test_singleton_pattern(self):
        """싱글톤 패턴이 동작하는지 검증."""
        from baldur.settings.slo import get_slo_settings

        settings1 = get_slo_settings()
        settings2 = get_slo_settings()

        assert settings1 is settings2
