"""
Unit tests for ThreadManagementSettings.

검증 항목:
- 설계 계약값 (기본값, 필드 수)
- 경계값 분석 (ge/le 제약)
- 환경 변수 오버라이드
- 싱글톤 캐싱/리셋 (Root 경유 SSOT)

테스트 대상: baldur.settings.thread_management
참조: 313_SETTINGS_CONFIGURATION_CONSISTENCY.md §2.1
"""

import os
from unittest import mock

import pytest
from pydantic import ValidationError

# =============================================================================
# 계약 검증: 설계 문서에 명시된 기본값
# =============================================================================


class TestThreadManagementSettingsContract:
    """ThreadManagementSettings 설계 계약값 검증.

    313 §2.1에 명시된 기본값 및 환경변수 프리픽스를 검증한다.
    """

    def test_join_timeout_default_is_5(self):
        """thread.join() 기본 타임아웃: 5.0초. 313 §2.1 설계 계약."""
        from baldur.settings.thread_management import (
            ThreadManagementSettings,
            reset_thread_management_settings,
        )

        reset_thread_management_settings()
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = ThreadManagementSettings()
            assert settings.join_timeout == 5.0

    def test_join_timeout_long_default_is_10(self):
        """장기 실행 thread join 타임아웃: 10.0초. 313 §2.1 설계 계약."""
        from baldur.settings.thread_management import (
            ThreadManagementSettings,
            reset_thread_management_settings,
        )

        reset_thread_management_settings()
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = ThreadManagementSettings()
            assert settings.join_timeout_long == 10.0

    def test_field_count_is_2(self):
        """ThreadManagementSettings는 2개 필드로 구성된다."""
        from baldur.settings.thread_management import ThreadManagementSettings

        assert len(ThreadManagementSettings.model_fields) == 2

    def test_env_prefix_is_baldur_thread(self):
        """환경변수 프리픽스: BALDUR_THREAD_MANAGEMENT_."""
        from baldur.settings.thread_management import ThreadManagementSettings

        assert (
            ThreadManagementSettings.model_config["env_prefix"]
            == "BALDUR_THREAD_MANAGEMENT_"
        )


# =============================================================================
# 경계값 분석: ge/le 제약
# =============================================================================


class TestThreadManagementSettingsBoundaryContract:
    """ThreadManagementSettings 필드 경계값 계약 검증."""

    def test_join_timeout_minimum_boundary(self):
        """join_timeout의 최소 경계: ge=1.0."""
        from baldur.settings.thread_management import ThreadManagementSettings

        with pytest.raises(ValidationError):
            ThreadManagementSettings(join_timeout=0.9)
        settings = ThreadManagementSettings(join_timeout=1.0)
        assert settings.join_timeout == 1.0

    def test_join_timeout_maximum_boundary(self):
        """join_timeout의 최대 경계: le=60.0."""
        from baldur.settings.thread_management import ThreadManagementSettings

        settings = ThreadManagementSettings(join_timeout=60.0)
        assert settings.join_timeout == 60.0
        with pytest.raises(ValidationError):
            ThreadManagementSettings(join_timeout=60.1)

    def test_join_timeout_long_minimum_boundary(self):
        """join_timeout_long의 최소 경계: ge=5.0."""
        from baldur.settings.thread_management import ThreadManagementSettings

        with pytest.raises(ValidationError):
            ThreadManagementSettings(join_timeout_long=4.9)
        settings = ThreadManagementSettings(join_timeout_long=5.0)
        assert settings.join_timeout_long == 5.0

    def test_join_timeout_long_maximum_boundary(self):
        """join_timeout_long의 최대 경계: le=120.0."""
        from baldur.settings.thread_management import ThreadManagementSettings

        settings = ThreadManagementSettings(join_timeout_long=120.0)
        assert settings.join_timeout_long == 120.0
        with pytest.raises(ValidationError):
            ThreadManagementSettings(join_timeout_long=120.1)


# =============================================================================
# 동작 검증: 환경변수 오버라이드 및 싱글톤
# =============================================================================


class TestThreadManagementSettingsBehavior:
    """ThreadManagementSettings 동작 검증."""

    @pytest.fixture(autouse=True)
    def _reset_singleton(self):
        from baldur.settings.thread_management import (
            reset_thread_management_settings,
        )

        reset_thread_management_settings()
        yield
        reset_thread_management_settings()

    def test_env_override_join_timeout(self):
        """BALDUR_THREAD_MANAGEMENT_JOIN_TIMEOUT 환경변수로 오버라이드."""
        from baldur.settings.thread_management import ThreadManagementSettings

        with mock.patch.dict(
            os.environ, {"BALDUR_THREAD_MANAGEMENT_JOIN_TIMEOUT": "8.0"}, clear=True
        ):
            settings = ThreadManagementSettings()
            assert settings.join_timeout == 8.0

    def test_env_override_join_timeout_long(self):
        """BALDUR_THREAD_MANAGEMENT_JOIN_TIMEOUT_LONG 환경변수로 오버라이드."""
        from baldur.settings.thread_management import ThreadManagementSettings

        with mock.patch.dict(
            os.environ,
            {"BALDUR_THREAD_MANAGEMENT_JOIN_TIMEOUT_LONG": "15.0"},
            clear=True,
        ):
            settings = ThreadManagementSettings()
            assert settings.join_timeout_long == 15.0

    def test_root_ssot_returns_thread_settings(self):
        """get_thread_management_settings()는 Root 경유 SSOT로 동작한다."""
        from baldur.settings.root import reset_config
        from baldur.settings.thread_management import (
            ThreadManagementSettings,
            get_thread_management_settings,
        )

        reset_config()
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = get_thread_management_settings()
            assert isinstance(settings, ThreadManagementSettings)

    def test_reset_clears_cached_root(self):
        """reset_thread_management_settings() 후 새 설정이 로드된다."""
        from baldur.settings.thread_management import (
            ThreadManagementSettings,
            get_thread_management_settings,
            reset_thread_management_settings,
        )

        reset_thread_management_settings()
        with mock.patch.dict(
            os.environ, {"BALDUR_THREAD_MANAGEMENT_JOIN_TIMEOUT": "7.0"}, clear=True
        ):
            s1 = get_thread_management_settings()
            assert s1.join_timeout == 7.0

        reset_thread_management_settings()
        with mock.patch.dict(os.environ, {}, clear=True):
            s2 = get_thread_management_settings()
            fresh = ThreadManagementSettings()
            assert s2.join_timeout == fresh.join_timeout
