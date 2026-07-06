"""
Tests for RegionalEmergencySettings Module.

테스트 범위:
- 기본값 검증 (cascade_detector.py, tracker.py 상수와 일치)
- 환경변수 오버라이드 검증
- 값 범위 유효성 검증
- 싱글톤 패턴 검증

Source:
    settings/regional_emergency.py
    services/regional_emergency/tracker.py (DEFAULT_EMERGENCY_EXPIRY_HOURS, CACHE_TTL_SECONDS)
    services/regional_emergency/cascade_detector.py (DEFAULT_ESCALATION_THRESHOLD, DEFAULT_CASCADE_WINDOW_MINUTES)
"""

import pytest
from pydantic import ValidationError


class TestRegionalEmergencySettingsDefaults:
    """RegionalEmergencySettings 기본값 테스트."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        """테스트 전후 싱글톤 초기화."""
        from baldur.settings.regional_emergency import (
            reset_regional_emergency_settings,
        )

        reset_regional_emergency_settings()
        yield
        reset_regional_emergency_settings()

    def test_cascade_detector_defaults(self):
        """cascade_detector.py의 상수와 일치하는지 검증.

        DEFAULT_ESCALATION_THRESHOLD = 2
        DEFAULT_CASCADE_WINDOW_MINUTES = 30
        """
        from baldur.settings.regional_emergency import RegionalEmergencySettings

        settings = RegionalEmergencySettings()

        assert settings.escalation_threshold == 2
        assert settings.cascade_window_minutes == 30

    def test_tracker_defaults(self):
        """tracker.py의 상수와 일치하는지 검증.

        DEFAULT_EMERGENCY_EXPIRY_HOURS = 8
        CACHE_TTL_SECONDS = 30.0
        """
        from baldur.settings.regional_emergency import RegionalEmergencySettings

        settings = RegionalEmergencySettings()

        assert settings.expiry_hours == 8
        assert settings.cache_ttl_seconds == 30.0

    def test_audit_trail_defaults(self):
        """escalation_audit.py의 기본값 검증.

        max_buffer_size = 1000
        """
        from baldur.settings.regional_emergency import RegionalEmergencySettings

        settings = RegionalEmergencySettings()

        assert settings.max_buffer_size == 1000


class TestRegionalEmergencySettingsEnvOverride:
    """환경변수 오버라이드 테스트."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        """테스트 전후 싱글톤 초기화."""
        from baldur.settings.regional_emergency import (
            reset_regional_emergency_settings,
        )

        reset_regional_emergency_settings()
        yield
        reset_regional_emergency_settings()

    def test_escalation_threshold_env_override(self, monkeypatch):
        """BALDUR_REGIONAL_EMERGENCY_ESCALATION_THRESHOLD 환경변수."""
        from baldur.settings.regional_emergency import RegionalEmergencySettings

        monkeypatch.setenv("BALDUR_REGIONAL_EMERGENCY_ESCALATION_THRESHOLD", "5")

        settings = RegionalEmergencySettings()

        assert settings.escalation_threshold == 5

    def test_cascade_window_minutes_env_override(self, monkeypatch):
        """BALDUR_REGIONAL_EMERGENCY_CASCADE_WINDOW_MINUTES 환경변수."""
        from baldur.settings.regional_emergency import RegionalEmergencySettings

        monkeypatch.setenv("BALDUR_REGIONAL_EMERGENCY_CASCADE_WINDOW_MINUTES", "60")

        settings = RegionalEmergencySettings()

        assert settings.cascade_window_minutes == 60

    def test_expiry_hours_env_override(self, monkeypatch):
        """BALDUR_REGIONAL_EMERGENCY_EXPIRY_HOURS 환경변수."""
        from baldur.settings.regional_emergency import RegionalEmergencySettings

        monkeypatch.setenv("BALDUR_REGIONAL_EMERGENCY_EXPIRY_HOURS", "24")

        settings = RegionalEmergencySettings()

        assert settings.expiry_hours == 24

    def test_cache_ttl_seconds_env_override(self, monkeypatch):
        """BALDUR_REGIONAL_EMERGENCY_CACHE_TTL_SECONDS 환경변수."""
        from baldur.settings.regional_emergency import RegionalEmergencySettings

        monkeypatch.setenv("BALDUR_REGIONAL_EMERGENCY_CACHE_TTL_SECONDS", "60.0")

        settings = RegionalEmergencySettings()

        assert settings.cache_ttl_seconds == 60.0

    def test_max_buffer_size_env_override(self, monkeypatch):
        """BALDUR_REGIONAL_EMERGENCY_MAX_BUFFER_SIZE 환경변수."""
        from baldur.settings.regional_emergency import RegionalEmergencySettings

        monkeypatch.setenv("BALDUR_REGIONAL_EMERGENCY_MAX_BUFFER_SIZE", "5000")

        settings = RegionalEmergencySettings()

        assert settings.max_buffer_size == 5000

    def test_multiple_env_overrides(self, monkeypatch):
        """여러 환경변수 동시 오버라이드."""
        from baldur.settings.regional_emergency import RegionalEmergencySettings

        monkeypatch.setenv("BALDUR_REGIONAL_EMERGENCY_ESCALATION_THRESHOLD", "3")
        monkeypatch.setenv("BALDUR_REGIONAL_EMERGENCY_CASCADE_WINDOW_MINUTES", "45")
        monkeypatch.setenv("BALDUR_REGIONAL_EMERGENCY_EXPIRY_HOURS", "12")
        monkeypatch.setenv("BALDUR_REGIONAL_EMERGENCY_CACHE_TTL_SECONDS", "15.0")

        settings = RegionalEmergencySettings()

        assert settings.escalation_threshold == 3
        assert settings.cascade_window_minutes == 45
        assert settings.expiry_hours == 12
        assert settings.cache_ttl_seconds == 15.0


class TestRegionalEmergencySettingsValidation:
    """값 범위 유효성 검증 테스트."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        """테스트 전후 싱글톤 초기화."""
        from baldur.settings.regional_emergency import (
            reset_regional_emergency_settings,
        )

        reset_regional_emergency_settings()
        yield
        reset_regional_emergency_settings()

    def test_escalation_threshold_min_max(self):
        """escalation_threshold 범위 (1-10) 검증."""
        from baldur.settings.regional_emergency import RegionalEmergencySettings

        # 최소값 허용
        settings = RegionalEmergencySettings(escalation_threshold=1)
        assert settings.escalation_threshold == 1

        # 최대값 허용
        settings = RegionalEmergencySettings(escalation_threshold=10)
        assert settings.escalation_threshold == 10

        # 범위 초과 시 ValidationError
        with pytest.raises(ValidationError):
            RegionalEmergencySettings(escalation_threshold=0)

        with pytest.raises(ValidationError):
            RegionalEmergencySettings(escalation_threshold=11)

    def test_cascade_window_minutes_min_max(self):
        """cascade_window_minutes 범위 (5-120) 검증."""
        from baldur.settings.regional_emergency import RegionalEmergencySettings

        # 최소값 허용
        settings = RegionalEmergencySettings(cascade_window_minutes=5)
        assert settings.cascade_window_minutes == 5

        # 최대값 허용
        settings = RegionalEmergencySettings(cascade_window_minutes=120)
        assert settings.cascade_window_minutes == 120

        # 범위 초과 시 ValidationError
        with pytest.raises(ValidationError):
            RegionalEmergencySettings(cascade_window_minutes=4)

        with pytest.raises(ValidationError):
            RegionalEmergencySettings(cascade_window_minutes=121)

    def test_expiry_hours_min_max(self):
        """expiry_hours 범위 (1-72) 검증."""
        from baldur.settings.regional_emergency import RegionalEmergencySettings

        # 최소값 허용
        settings = RegionalEmergencySettings(expiry_hours=1)
        assert settings.expiry_hours == 1

        # 최대값 허용
        settings = RegionalEmergencySettings(expiry_hours=72)
        assert settings.expiry_hours == 72

        # 범위 초과 시 ValidationError
        with pytest.raises(ValidationError):
            RegionalEmergencySettings(expiry_hours=0)

        with pytest.raises(ValidationError):
            RegionalEmergencySettings(expiry_hours=73)

    def test_cache_ttl_seconds_min_max(self):
        """cache_ttl_seconds 범위 (1.0-300.0) 검증."""
        from baldur.settings.regional_emergency import RegionalEmergencySettings

        # 최소값 허용
        settings = RegionalEmergencySettings(cache_ttl_seconds=1.0)
        assert settings.cache_ttl_seconds == 1.0

        # 최대값 허용
        settings = RegionalEmergencySettings(cache_ttl_seconds=300.0)
        assert settings.cache_ttl_seconds == 300.0

        # 범위 초과 시 ValidationError
        with pytest.raises(ValidationError):
            RegionalEmergencySettings(cache_ttl_seconds=0.5)

        with pytest.raises(ValidationError):
            RegionalEmergencySettings(cache_ttl_seconds=301.0)

    def test_max_buffer_size_min_max(self):
        """max_buffer_size 범위 (100-100000) 검증."""
        from baldur.settings.regional_emergency import RegionalEmergencySettings

        # 최소값 허용
        settings = RegionalEmergencySettings(max_buffer_size=100)
        assert settings.max_buffer_size == 100

        # 최대값 허용
        settings = RegionalEmergencySettings(max_buffer_size=100000)
        assert settings.max_buffer_size == 100000

        # 범위 초과 시 ValidationError
        with pytest.raises(ValidationError):
            RegionalEmergencySettings(max_buffer_size=99)

        with pytest.raises(ValidationError):
            RegionalEmergencySettings(max_buffer_size=100001)


class TestRegionalEmergencySettingsSingleton:
    """싱글톤 패턴 테스트."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        """테스트 전후 싱글톤 초기화."""
        from baldur.settings.regional_emergency import (
            reset_regional_emergency_settings,
        )

        reset_regional_emergency_settings()
        yield
        reset_regional_emergency_settings()

    def test_singleton_returns_same_instance(self):
        """싱글톤이 동일한 인스턴스 반환."""
        from baldur.settings.regional_emergency import (
            get_regional_emergency_settings,
        )

        settings1 = get_regional_emergency_settings()
        settings2 = get_regional_emergency_settings()

        assert settings1 is settings2

    def test_reset_creates_new_instance(self):
        """reset 후 새 인스턴스 생성."""
        from baldur.settings.regional_emergency import (
            get_regional_emergency_settings,
            reset_regional_emergency_settings,
        )

        settings1 = get_regional_emergency_settings()
        reset_regional_emergency_settings()
        settings2 = get_regional_emergency_settings()

        assert settings1 is not settings2

    def test_reset_applies_new_env_values(self, monkeypatch):
        """reset 후 새 환경변수 값 적용."""
        from baldur.settings.regional_emergency import (
            get_regional_emergency_settings,
            reset_regional_emergency_settings,
        )

        # 초기값 확인
        settings1 = get_regional_emergency_settings()
        original_threshold = settings1.escalation_threshold

        # 환경변수 설정 후 reset
        monkeypatch.setenv("BALDUR_REGIONAL_EMERGENCY_ESCALATION_THRESHOLD", "7")
        reset_regional_emergency_settings()
        settings2 = get_regional_emergency_settings()

        assert settings2.escalation_threshold == 7
        assert settings2.escalation_threshold != original_threshold


class TestRegionalEmergencySettingsModuleExports:
    """모듈 export 검증 테스트."""

    def test_exports_available(self):
        """필수 export 함수/클래스 존재 확인."""
        from baldur.settings import regional_emergency

        assert hasattr(regional_emergency, "RegionalEmergencySettings")
        assert hasattr(regional_emergency, "get_regional_emergency_settings")
        assert hasattr(regional_emergency, "reset_regional_emergency_settings")
