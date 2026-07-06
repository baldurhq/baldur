"""
AdmissionControlSettings Bulkhead Timeout 필드 단위 테스트.

테스트 항목:
- 계약: tier별 Bulkhead Timeout 기본값
- 동작: get_tier_bulkhead_timeout() 반환값 및 Zero-Wait → None 변환
"""

import pytest

from baldur.settings.admission_control import (
    AdmissionControlSettings,
    reset_admission_control_settings,
)


class TestBulkheadTimeoutContract:
    """Tier별 Bulkhead Timeout 설계 계약값 검증."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        reset_admission_control_settings()
        yield
        reset_admission_control_settings()

    def test_critical_timeout_default(self):
        """critical tier Bulkhead timeout 기본값은 0.05초."""
        settings = AdmissionControlSettings()
        assert settings.tier_critical_bulkhead_timeout_seconds == 0.05

    def test_standard_timeout_default(self):
        """standard tier Bulkhead timeout 기본값은 0.03초."""
        settings = AdmissionControlSettings()
        assert settings.tier_standard_bulkhead_timeout_seconds == 0.03

    def test_non_essential_timeout_default(self):
        """non_essential tier Bulkhead timeout 기본값은 0.0 (Zero-Wait)."""
        settings = AdmissionControlSettings()
        assert settings.tier_non_essential_bulkhead_timeout_seconds == 0.0


class TestGetTierBulkheadTimeoutBehavior:
    """get_tier_bulkhead_timeout() 동작 검증."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        reset_admission_control_settings()
        yield
        reset_admission_control_settings()

    def test_critical_returns_positive_float(self):
        """critical tier는 설정된 positive float을 반환한다."""
        settings = AdmissionControlSettings()
        result = settings.get_tier_bulkhead_timeout("critical")
        assert result == settings.tier_critical_bulkhead_timeout_seconds

    def test_standard_returns_positive_float(self):
        """standard tier는 설정된 positive float을 반환한다."""
        settings = AdmissionControlSettings()
        result = settings.get_tier_bulkhead_timeout("standard")
        assert result == settings.tier_standard_bulkhead_timeout_seconds

    def test_non_essential_returns_none(self):
        """non_essential tier는 Zero-Wait(0.0)이므로 None을 반환한다."""
        settings = AdmissionControlSettings()
        result = settings.get_tier_bulkhead_timeout("non_essential")
        assert result is None

    def test_unknown_tier_returns_none(self):
        """알 수 없는 tier는 None(즉시 실패)을 반환한다."""
        settings = AdmissionControlSettings()
        result = settings.get_tier_bulkhead_timeout("unknown")
        assert result is None

    def test_custom_zero_converts_to_none(self):
        """timeout을 0으로 설정하면 None을 반환한다."""
        settings = AdmissionControlSettings(
            tier_critical_bulkhead_timeout_seconds=0.0,
        )
        assert settings.get_tier_bulkhead_timeout("critical") is None

    def test_custom_positive_value_returned(self):
        """양수 timeout을 설정하면 해당 값을 반환한다."""
        settings = AdmissionControlSettings(
            tier_non_essential_bulkhead_timeout_seconds=0.1,
        )
        assert settings.get_tier_bulkhead_timeout("non_essential") == 0.1
