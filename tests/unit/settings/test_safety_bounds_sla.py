"""
SafetyBounds SLA 파라미터 한계값 단위 테스트.

throttle_sla_warning_ms, throttle_sla_critical_ms가
SafetyBoundsSettings와 SafetyBounds에 올바르게 등록되었는지 검증한다.

테스트 분류:
- 계약 검증 (Contract): 필드 존재 + 설계 사양의 구체적 기본값을 하드코딩으로 고정
- 동작 검증 (Behavior): get_bounds/is_within_bounds 동작을 소스 참조로 검증
"""

from __future__ import annotations

import pytest

from baldur.core.safety_bounds import ParameterBound, SafetyBounds
from baldur.settings.safety_bounds import (
    SafetyBoundsSettings,
    reset_safety_bounds_settings,
)


@pytest.fixture(autouse=True)
def _reset_settings():
    """테스트 격리를 위해 설정 싱글톤 초기화."""
    reset_safety_bounds_settings()
    yield
    reset_safety_bounds_settings()


@pytest.fixture
def default_settings():
    """기본 SafetyBoundsSettings 인스턴스 (소스 기본값 참조용)."""
    return SafetyBoundsSettings()


# =============================================================================
# 계약 검증 (Contract Tests) — 설계 사양의 구체적 값을 하드코딩으로 고정
# =============================================================================


class TestSafetyBoundsSettingsSlaContract:
    """SafetyBoundsSettings SLA 필드 설계 계약 검증 (하드코딩 허용)."""

    def test_throttle_sla_warning_ms_fields_exist(self):
        """throttle_sla_warning_ms 관련 3개 필드가 존재해야 한다."""
        settings = SafetyBoundsSettings()
        assert hasattr(settings, "throttle_sla_warning_ms_min")
        assert hasattr(settings, "throttle_sla_warning_ms_max")
        assert hasattr(settings, "throttle_sla_warning_ms_max_change")

    def test_throttle_sla_critical_ms_fields_exist(self):
        """throttle_sla_critical_ms 관련 3개 필드가 존재해야 한다."""
        settings = SafetyBoundsSettings()
        assert hasattr(settings, "throttle_sla_critical_ms_min")
        assert hasattr(settings, "throttle_sla_critical_ms_max")
        assert hasattr(settings, "throttle_sla_critical_ms_max_change")

    def test_sla_warning_contract_values(self):
        """SLA Warning 설계 계약값: min=50, max=2000, max_change=0.3."""
        settings = SafetyBoundsSettings()
        assert settings.throttle_sla_warning_ms_min == 50
        assert settings.throttle_sla_warning_ms_max == 2000
        assert settings.throttle_sla_warning_ms_max_change == 0.3

    def test_sla_critical_contract_values(self):
        """SLA Critical 설계 계약값: min=100, max=5000, max_change=0.3."""
        settings = SafetyBoundsSettings()
        assert settings.throttle_sla_critical_ms_min == 100
        assert settings.throttle_sla_critical_ms_max == 5000
        assert settings.throttle_sla_critical_ms_max_change == 0.3


class TestSafetyBoundsSlaRegistrationContract:
    """SafetyBounds SLA 파라미터 등록 계약 검증."""

    def test_default_bounds_include_sla_warning(self):
        """_get_default_bounds()에 throttle_sla_warning_ms가 포함되어야 한다."""
        bounds = SafetyBounds._get_default_bounds()
        assert "throttle_sla_warning_ms" in bounds
        assert isinstance(bounds["throttle_sla_warning_ms"], ParameterBound)

    def test_default_bounds_include_sla_critical(self):
        """_get_default_bounds()에 throttle_sla_critical_ms가 포함되어야 한다."""
        bounds = SafetyBounds._get_default_bounds()
        assert "throttle_sla_critical_ms" in bounds
        assert isinstance(bounds["throttle_sla_critical_ms"], ParameterBound)


# =============================================================================
# 동작 검증 (Behavior Tests) — 소스 참조로 기능 동작을 검증
# =============================================================================


class TestSafetyBoundsSettingsSlaBehavior:
    """SafetyBoundsSettings SLA 동작 검증 (소스 참조)."""

    def test_get_bounds_sla_warning(self, default_settings):
        """get_bounds("throttle_sla_warning_ms")가 올바른 값을 반환해야 한다."""
        bound_config = default_settings.get_bounds("throttle_sla_warning_ms")

        assert bound_config is not None
        assert bound_config.min_value == default_settings.throttle_sla_warning_ms_min
        assert bound_config.max_value == default_settings.throttle_sla_warning_ms_max
        assert (
            bound_config.max_change_per_cycle
            == default_settings.throttle_sla_warning_ms_max_change
        )

    def test_get_bounds_sla_critical(self, default_settings):
        """get_bounds("throttle_sla_critical_ms")가 올바른 값을 반환해야 한다."""
        bound_config = default_settings.get_bounds("throttle_sla_critical_ms")

        assert bound_config is not None
        assert bound_config.min_value == default_settings.throttle_sla_critical_ms_min
        assert bound_config.max_value == default_settings.throttle_sla_critical_ms_max
        assert (
            bound_config.max_change_per_cycle
            == default_settings.throttle_sla_critical_ms_max_change
        )


class TestSafetyBoundsSlaParamBehavior:
    """SafetyBounds SLA 파라미터 동작 검증 (소스 참조)."""

    def test_is_within_bounds_sla_warning_valid(self, default_settings):
        """유효한 SLA Warning 값은 범위 내로 판정되어야 한다."""
        safety = SafetyBounds()
        mid_value = (
            default_settings.throttle_sla_warning_ms_min
            + default_settings.throttle_sla_warning_ms_max
        ) / 2
        assert safety.is_within_bounds("throttle_sla_warning_ms", mid_value) is True

    def test_is_within_bounds_sla_warning_below_min(self, default_settings):
        """SLA Warning 최소값 미만은 범위 외로 판정되어야 한다."""
        safety = SafetyBounds()
        below_min = default_settings.throttle_sla_warning_ms_min - 1
        assert safety.is_within_bounds("throttle_sla_warning_ms", below_min) is False

    def test_is_within_bounds_sla_warning_above_max(self, default_settings):
        """SLA Warning 최대값 초과는 범위 외로 판정되어야 한다."""
        safety = SafetyBounds()
        above_max = default_settings.throttle_sla_warning_ms_max + 1000
        assert safety.is_within_bounds("throttle_sla_warning_ms", above_max) is False

    def test_is_within_bounds_sla_critical_valid(self, default_settings):
        """유효한 SLA Critical 값은 범위 내로 판정되어야 한다."""
        safety = SafetyBounds()
        mid_value = (
            default_settings.throttle_sla_critical_ms_min
            + default_settings.throttle_sla_critical_ms_max
        ) / 2
        assert safety.is_within_bounds("throttle_sla_critical_ms", mid_value) is True

    def test_is_within_bounds_sla_critical_below_min(self, default_settings):
        """SLA Critical 최소값 미만은 범위 외로 판정되어야 한다."""
        safety = SafetyBounds()
        below_min = default_settings.throttle_sla_critical_ms_min - 1
        assert safety.is_within_bounds("throttle_sla_critical_ms", below_min) is False

    def test_is_within_bounds_sla_critical_above_max(self, default_settings):
        """SLA Critical 최대값 초과는 범위 외로 판정되어야 한다."""
        safety = SafetyBounds()
        above_max = default_settings.throttle_sla_critical_ms_max + 1000
        assert safety.is_within_bounds("throttle_sla_critical_ms", above_max) is False

    def test_sla_warning_bound_values_from_settings(self, default_settings):
        """SafetyBounds의 SLA Warning bound 값이 Settings와 일치해야 한다."""
        safety = SafetyBounds()

        bound = safety.bounds["throttle_sla_warning_ms"]
        assert bound.min_value == default_settings.throttle_sla_warning_ms_min
        assert bound.max_value == default_settings.throttle_sla_warning_ms_max
        assert (
            bound.max_change_per_cycle
            == default_settings.throttle_sla_warning_ms_max_change
        )

    def test_sla_critical_bound_values_from_settings(self, default_settings):
        """SafetyBounds의 SLA Critical bound 값이 Settings와 일치해야 한다."""
        safety = SafetyBounds()

        bound = safety.bounds["throttle_sla_critical_ms"]
        assert bound.min_value == default_settings.throttle_sla_critical_ms_min
        assert bound.max_value == default_settings.throttle_sla_critical_ms_max
        assert (
            bound.max_change_per_cycle
            == default_settings.throttle_sla_critical_ms_max_change
        )
