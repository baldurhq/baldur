"""
Unit tests for PostmortemSettings.

설정 검증:
- 기본값 확인
- 환경 변수 로딩
- 필드 유효성 검증

테스트 대상: baldur.settings.postmortem
"""

import os
from unittest import mock

import pytest


class TestPostmortemSettingsDefaults:
    """PostmortemSettings 기본값 테스트."""

    def test_default_history_limit(self):
        """history_limit 기본값은 100."""
        from baldur.settings.postmortem import (
            PostmortemSettings,
            reset_postmortem_settings,
        )

        reset_postmortem_settings()
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = PostmortemSettings()
            assert settings.history_limit == 100

    def test_default_auto_enabled(self):
        """auto_enabled 기본값은 False."""
        from baldur.settings.postmortem import (
            PostmortemSettings,
            reset_postmortem_settings,
        )

        reset_postmortem_settings()
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = PostmortemSettings()
            assert settings.auto_enabled is False

    def test_default_auto_min_duration(self):
        """auto_min_duration 기본값은 30."""
        from baldur.settings.postmortem import (
            PostmortemSettings,
            reset_postmortem_settings,
        )

        reset_postmortem_settings()
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = PostmortemSettings()
            assert settings.auto_min_duration == 30

    def test_default_notification_enabled(self):
        """notification_enabled 기본값은 False (impl 527, v1.1 deferred)."""
        from baldur.settings.postmortem import (
            PostmortemSettings,
            reset_postmortem_settings,
        )

        reset_postmortem_settings()
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = PostmortemSettings()
            assert settings.notification_enabled is False

    def test_default_notification_min_duration(self):
        """notification_min_duration 기본값은 60."""
        from baldur.settings.postmortem import (
            PostmortemSettings,
            reset_postmortem_settings,
        )

        reset_postmortem_settings()
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = PostmortemSettings()
            assert settings.notification_min_duration == 60

    def test_default_incidents_default_limit(self):
        """incidents_default_limit 기본값은 10."""
        from baldur.settings.postmortem import (
            PostmortemSettings,
            reset_postmortem_settings,
        )

        reset_postmortem_settings()
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = PostmortemSettings()
            assert settings.incidents_default_limit == 10


class TestPostmortemSettingsEnvVariables:
    """환경 변수를 통한 설정 로딩 테스트."""

    def test_load_history_limit_from_env(self):
        """BALDUR_POSTMORTEM_HISTORY_LIMIT 환경 변수 로딩."""
        from baldur.settings.postmortem import (
            PostmortemSettings,
            reset_postmortem_settings,
        )

        reset_postmortem_settings()
        with mock.patch.dict(os.environ, {"BALDUR_POSTMORTEM_HISTORY_LIMIT": "200"}):
            settings = PostmortemSettings()
            assert settings.history_limit == 200

    def test_load_auto_enabled_from_env(self):
        """BALDUR_POSTMORTEM_AUTO_ENABLED 환경 변수 로딩."""
        from baldur.settings.postmortem import (
            PostmortemSettings,
            reset_postmortem_settings,
        )

        reset_postmortem_settings()
        with mock.patch.dict(os.environ, {"BALDUR_POSTMORTEM_AUTO_ENABLED": "true"}):
            settings = PostmortemSettings()
            assert settings.auto_enabled is True

    def test_load_auto_min_duration_from_env(self):
        """BALDUR_POSTMORTEM_AUTO_MIN_DURATION 환경 변수 로딩."""
        from baldur.settings.postmortem import (
            PostmortemSettings,
            reset_postmortem_settings,
        )

        reset_postmortem_settings()
        with mock.patch.dict(os.environ, {"BALDUR_POSTMORTEM_AUTO_MIN_DURATION": "60"}):
            settings = PostmortemSettings()
            assert settings.auto_min_duration == 60

    def test_load_notification_enabled_from_env(self):
        """BALDUR_POSTMORTEM_NOTIFICATION_ENABLED 환경 변수 로딩."""
        from baldur.settings.postmortem import (
            PostmortemSettings,
            reset_postmortem_settings,
        )

        reset_postmortem_settings()
        with mock.patch.dict(
            os.environ, {"BALDUR_POSTMORTEM_NOTIFICATION_ENABLED": "false"}
        ):
            settings = PostmortemSettings()
            assert settings.notification_enabled is False

    def test_load_incidents_default_limit_from_env(self):
        """BALDUR_POSTMORTEM_INCIDENTS_DEFAULT_LIMIT 환경 변수 로딩."""
        from baldur.settings.postmortem import (
            PostmortemSettings,
            reset_postmortem_settings,
        )

        reset_postmortem_settings()
        with mock.patch.dict(
            os.environ, {"BALDUR_POSTMORTEM_INCIDENTS_DEFAULT_LIMIT": "50"}
        ):
            settings = PostmortemSettings()
            assert settings.incidents_default_limit == 50


class TestPostmortemSettingsValidation:
    """설정값 유효성 검증 테스트."""

    def test_history_limit_min_value(self):
        """history_limit 최소값 검증 (50 이상)."""
        from pydantic import ValidationError

        from baldur.settings.postmortem import (
            PostmortemSettings,
            reset_postmortem_settings,
        )

        reset_postmortem_settings()
        with mock.patch.dict(os.environ, {"BALDUR_POSTMORTEM_HISTORY_LIMIT": "10"}):
            with pytest.raises(ValidationError):
                PostmortemSettings()

    def test_history_limit_max_value(self):
        """history_limit 최대값 검증 (500 이하)."""
        from pydantic import ValidationError

        from baldur.settings.postmortem import (
            PostmortemSettings,
            reset_postmortem_settings,
        )

        reset_postmortem_settings()
        with mock.patch.dict(os.environ, {"BALDUR_POSTMORTEM_HISTORY_LIMIT": "600"}):
            with pytest.raises(ValidationError):
                PostmortemSettings()

    def test_auto_min_duration_max_value(self):
        """auto_min_duration 최대값 검증 (3600 이하)."""
        from pydantic import ValidationError

        from baldur.settings.postmortem import (
            PostmortemSettings,
            reset_postmortem_settings,
        )

        reset_postmortem_settings()
        with mock.patch.dict(
            os.environ, {"BALDUR_POSTMORTEM_AUTO_MIN_DURATION": "4000"}
        ):
            with pytest.raises(ValidationError):
                PostmortemSettings()


class TestPostmortemSettingsSingleton:
    """Singleton 패턴 테스트."""

    def test_get_postmortem_settings_returns_same_instance(self):
        """get_postmortem_settings()가 동일 인스턴스 반환."""
        from baldur.settings.postmortem import (
            get_postmortem_settings,
            reset_postmortem_settings,
        )

        reset_postmortem_settings()
        settings1 = get_postmortem_settings()
        settings2 = get_postmortem_settings()
        assert settings1 is settings2

    def test_reset_postmortem_settings_clears_singleton(self):
        """reset_postmortem_settings()가 싱글톤 초기화."""
        from baldur.settings.postmortem import (
            get_postmortem_settings,
            reset_postmortem_settings,
        )

        settings1 = get_postmortem_settings()
        reset_postmortem_settings()
        settings2 = get_postmortem_settings()
        # 새로운 인스턴스여야 함 (reset 후)
        assert settings1 is not settings2
