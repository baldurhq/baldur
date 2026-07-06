"""
Unit tests for AdmissionControlSettings (236 작업 2).

테스트 항목:
- 계약 검증: 기본값은 설계 문서에 명시된 값
- 동작 검증: get_tier_max_concurrent() 메서드 동작
- 싱글톤: get/reset 동작
"""

import pytest

from baldur.settings.admission_control import (
    AdmissionControlSettings,
    get_admission_control_settings,
    reset_admission_control_settings,
)


class TestAdmissionControlSettingsContract:
    """AdmissionControlSettings 설계 계약값 검증."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        reset_admission_control_settings()
        yield
        reset_admission_control_settings()

    def test_enabled_default(self):
        """enabled 기본값은 True."""
        settings = AdmissionControlSettings()
        assert settings.enabled is True

    def test_tier_critical_max_concurrent_default(self):
        """critical tier 기본 동시 실행 수는 100."""
        settings = AdmissionControlSettings()
        assert settings.tier_critical_max_concurrent == 100

    def test_tier_standard_max_concurrent_default(self):
        """standard tier 기본 동시 실행 수는 50."""
        settings = AdmissionControlSettings()
        assert settings.tier_standard_max_concurrent == 50

    def test_tier_non_essential_max_concurrent_default(self):
        """non_essential tier 기본 동시 실행 수는 20."""
        settings = AdmissionControlSettings()
        assert settings.tier_non_essential_max_concurrent == 20

    def test_env_prefix(self):
        """환경변수 prefix는 BALDUR_ADMISSION_CONTROL_."""
        config = AdmissionControlSettings.model_config
        assert config["env_prefix"] == "BALDUR_ADMISSION_CONTROL_"


class TestAdmissionControlSettingsBehavior:
    """AdmissionControlSettings 동작 검증."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        reset_admission_control_settings()
        yield
        reset_admission_control_settings()

    def test_get_tier_max_concurrent_critical(self):
        """get_tier_max_concurrent('critical')은 tier_critical_max_concurrent를 반환."""
        settings = AdmissionControlSettings()
        assert (
            settings.get_tier_max_concurrent("critical")
            == settings.tier_critical_max_concurrent
        )

    def test_get_tier_max_concurrent_standard(self):
        """get_tier_max_concurrent('standard')은 tier_standard_max_concurrent를 반환."""
        settings = AdmissionControlSettings()
        assert (
            settings.get_tier_max_concurrent("standard")
            == settings.tier_standard_max_concurrent
        )

    def test_get_tier_max_concurrent_non_essential(self):
        """get_tier_max_concurrent('non_essential')은 tier_non_essential_max_concurrent를 반환."""
        settings = AdmissionControlSettings()
        assert (
            settings.get_tier_max_concurrent("non_essential")
            == settings.tier_non_essential_max_concurrent
        )

    def test_get_tier_max_concurrent_unknown_falls_back_to_standard(self):
        """알 수 없는 tier_id는 standard 값을 반환한다."""
        settings = AdmissionControlSettings()
        assert (
            settings.get_tier_max_concurrent("unknown")
            == settings.tier_standard_max_concurrent
        )

    def test_custom_values(self):
        """커스텀 값 설정 시 반영."""
        settings = AdmissionControlSettings(
            tier_critical_max_concurrent=200,
            tier_standard_max_concurrent=80,
            tier_non_essential_max_concurrent=10,
        )
        assert settings.get_tier_max_concurrent("critical") == 200
        assert settings.get_tier_max_concurrent("standard") == 80
        assert settings.get_tier_max_concurrent("non_essential") == 10


class TestAdmissionControlSettingsSingleton:
    """AdmissionControlSettings 싱글톤 테스트."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        reset_admission_control_settings()
        yield
        reset_admission_control_settings()

    def test_singleton_returns_same_instance(self):
        """싱글톤이 동일한 인스턴스를 반환한다."""
        s1 = get_admission_control_settings()
        s2 = get_admission_control_settings()
        assert s1 is s2

    def test_reset_creates_new_instance(self):
        """리셋 후 새 인스턴스가 생성된다."""
        s1 = get_admission_control_settings()
        reset_admission_control_settings()
        s2 = get_admission_control_settings()
        assert s1 is not s2
