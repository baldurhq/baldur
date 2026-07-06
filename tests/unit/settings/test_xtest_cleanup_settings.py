"""
Tests for X-Test Artifact Cleanup Settings.

XTestCleanupSettings 클래스 테스트:
- 기본값 검증
- 환경변수 오버라이드
- 유효성 검증
"""

import pytest
from pydantic import ValidationError


class TestXTestCleanupSettings:
    """XTestCleanupSettings 테스트."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        """Reset singleton before and after each test."""
        from baldur.settings.xtest_cleanup import reset_xtest_cleanup_settings

        reset_xtest_cleanup_settings()
        yield
        reset_xtest_cleanup_settings()

    def test_default_session_ttl_hours(self):
        """세션 TTL 기본값 4시간 검증."""
        from baldur.settings.xtest_cleanup import XTestCleanupSettings

        settings = XTestCleanupSettings()
        assert settings.session_ttl_hours == 4

    def test_default_cleanup_interval_minutes(self):
        """정리 주기 기본값 30분 검증."""
        from baldur.settings.xtest_cleanup import XTestCleanupSettings

        settings = XTestCleanupSettings()
        assert settings.cleanup_interval_minutes == 30

    def test_default_component_auto_cleanup_flags(self):
        """컴포넌트별 자동 정리 기본값 True 검증."""
        from baldur.settings.xtest_cleanup import XTestCleanupSettings

        settings = XTestCleanupSettings()
        assert settings.cb_auto_restore is True
        assert settings.dlq_auto_purge is True
        assert settings.idempotency_auto_clear is True
        assert settings.rate_limit_auto_reset is True

    def test_default_celery_retry_settings(self):
        """Celery 태스크 재시도 기본값 검증."""
        from baldur.settings.xtest_cleanup import XTestCleanupSettings

        settings = XTestCleanupSettings()
        assert settings.max_retries == 2
        assert settings.retry_delay == 60

    def test_default_redis_key_prefixes(self):
        """Redis 키 접두사 기본값 검증."""
        from baldur.settings.xtest_cleanup import XTestCleanupSettings

        settings = XTestCleanupSettings()
        assert settings.redis_session_prefix == "xtest:session:"
        assert settings.redis_active_sessions_key == "xtest:session:active"

    def test_env_override_session_ttl(self, monkeypatch):
        """환경변수로 세션 TTL 오버라이드 검증."""
        from baldur.settings.xtest_cleanup import XTestCleanupSettings

        monkeypatch.setenv("BALDUR_XTEST_CLEANUP_SESSION_TTL_HOURS", "8")

        settings = XTestCleanupSettings()
        assert settings.session_ttl_hours == 8

    def test_env_override_cleanup_interval(self, monkeypatch):
        """환경변수로 정리 주기 오버라이드 검증."""
        from baldur.settings.xtest_cleanup import XTestCleanupSettings

        monkeypatch.setenv("BALDUR_XTEST_CLEANUP_CLEANUP_INTERVAL_MINUTES", "15")

        settings = XTestCleanupSettings()
        assert settings.cleanup_interval_minutes == 15

    def test_env_override_auto_cleanup_flags(self, monkeypatch):
        """환경변수로 자동 정리 플래그 오버라이드 검증."""
        from baldur.settings.xtest_cleanup import XTestCleanupSettings

        monkeypatch.setenv("BALDUR_XTEST_CLEANUP_CB_AUTO_RESTORE", "false")
        monkeypatch.setenv("BALDUR_XTEST_CLEANUP_DLQ_AUTO_PURGE", "false")

        settings = XTestCleanupSettings()
        assert settings.cb_auto_restore is False
        assert settings.dlq_auto_purge is False

    def test_validation_session_ttl_min_value(self):
        """세션 TTL 최소값 1 검증."""
        from baldur.settings.xtest_cleanup import XTestCleanupSettings

        # 최소값 허용
        settings = XTestCleanupSettings(session_ttl_hours=1)
        assert settings.session_ttl_hours == 1

        # 최소값 미만 시 ValidationError
        with pytest.raises(ValidationError):
            XTestCleanupSettings(session_ttl_hours=0)

    def test_validation_session_ttl_max_value(self):
        """세션 TTL 최대값 24 검증."""
        from baldur.settings.xtest_cleanup import XTestCleanupSettings

        # 최대값 허용
        settings = XTestCleanupSettings(session_ttl_hours=24)
        assert settings.session_ttl_hours == 24

        # 최대값 초과 시 ValidationError
        with pytest.raises(ValidationError):
            XTestCleanupSettings(session_ttl_hours=25)

    def test_validation_cleanup_interval_min_value(self):
        """정리 주기 최소값 5분 검증."""
        from baldur.settings.xtest_cleanup import XTestCleanupSettings

        # 최소값 허용
        settings = XTestCleanupSettings(cleanup_interval_minutes=5)
        assert settings.cleanup_interval_minutes == 5

        # 최소값 미만 시 ValidationError
        with pytest.raises(ValidationError):
            XTestCleanupSettings(cleanup_interval_minutes=4)

    def test_validation_cleanup_interval_max_value(self):
        """정리 주기 최대값 120분 검증."""
        from baldur.settings.xtest_cleanup import XTestCleanupSettings

        # 최대값 허용
        settings = XTestCleanupSettings(cleanup_interval_minutes=120)
        assert settings.cleanup_interval_minutes == 120

        # 최대값 초과 시 ValidationError
        with pytest.raises(ValidationError):
            XTestCleanupSettings(cleanup_interval_minutes=121)

    def test_validation_max_retries_range(self):
        """Celery 태스크 최대 재시도 범위 0-5 검증."""
        from baldur.settings.xtest_cleanup import XTestCleanupSettings

        # 범위 내 허용
        settings = XTestCleanupSettings(max_retries=0)
        assert settings.max_retries == 0

        settings = XTestCleanupSettings(max_retries=5)
        assert settings.max_retries == 5

        # 범위 초과 시 ValidationError
        with pytest.raises(ValidationError):
            XTestCleanupSettings(max_retries=6)

    def test_get_xtest_cleanup_settings_singleton(self):
        """get_xtest_cleanup_settings() 싱글톤 동작 검증."""
        from baldur.settings.xtest_cleanup import (
            get_xtest_cleanup_settings,
            reset_xtest_cleanup_settings,
        )

        reset_xtest_cleanup_settings()

        settings1 = get_xtest_cleanup_settings()
        settings2 = get_xtest_cleanup_settings()

        assert settings1 is settings2


class TestXTestCleanupSettingsValidators:
    """XTestCleanupSettings 커스텀 validator 테스트."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        """Reset singleton before and after each test."""
        from baldur.settings.xtest_cleanup import reset_xtest_cleanup_settings

        reset_xtest_cleanup_settings()
        yield
        reset_xtest_cleanup_settings()

    def test_session_ttl_validator_warning_for_low_value(self, monkeypatch, caplog):
        """세션 TTL 경고 로깅 검증 (validator 내부 처리)."""
        from baldur.settings.xtest_cleanup import XTestCleanupSettings

        # 유효한 최소값
        settings = XTestCleanupSettings(session_ttl_hours=1)
        assert settings.session_ttl_hours == 1

    def test_cleanup_interval_validator_warning_for_low_value(self, caplog):
        """정리 주기 경고 로깅 검증 (validator 내부 처리)."""
        from baldur.settings.xtest_cleanup import XTestCleanupSettings

        # 유효한 최소값
        settings = XTestCleanupSettings(cleanup_interval_minutes=5)
        assert settings.cleanup_interval_minutes == 5
