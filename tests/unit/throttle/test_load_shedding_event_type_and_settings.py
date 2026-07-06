"""
EventType.LOAD_SHEDDING_LEVEL_CHANGED 및 ThrottleSettings.shedding_compensation_factor 단위 테스트.

테스트 대상:
1. EventType enum에 LOAD_SHEDDING_LEVEL_CHANGED 존재
2. ThrottleSettings.shedding_compensation_factor 기본값, 범위, 설명
"""

import pytest

from baldur.services.event_bus.bus import EventType
from baldur.settings.throttle import ThrottleSettings


class TestLoadSheddingEventType:
    """EventType.LOAD_SHEDDING_LEVEL_CHANGED 검증."""

    def test_enum_member_exists(self):
        """LOAD_SHEDDING_LEVEL_CHANGED enum 멤버 존재."""
        assert hasattr(EventType, "LOAD_SHEDDING_LEVEL_CHANGED")

    def test_enum_value(self):
        """LOAD_SHEDDING_LEVEL_CHANGED 문자열 값."""
        assert (
            EventType.LOAD_SHEDDING_LEVEL_CHANGED.value == "load_shedding_level_changed"
        )

    def test_is_string_enum(self):
        """EventType은 str Enum이므로 문자열 비교 가능."""
        assert EventType.LOAD_SHEDDING_LEVEL_CHANGED == "load_shedding_level_changed"


class TestSheddingCompensationFactor:
    """ThrottleSettings.shedding_compensation_factor 검증."""

    def test_default_value(self):
        """기본값 = 1.5."""
        field_info = ThrottleSettings.model_fields["shedding_compensation_factor"]
        assert field_info.default == 1.5

    def test_settings_instance_default(self):
        """인스턴스 생성 시 기본값 적용."""
        settings = ThrottleSettings()
        expected_default = ThrottleSettings.model_fields[
            "shedding_compensation_factor"
        ].default
        assert settings.shedding_compensation_factor == expected_default

    def test_min_value_constraint(self):
        """최소값 1.0 미만 시 ValidationError."""
        with pytest.raises(Exception):
            ThrottleSettings(shedding_compensation_factor=0.5)

    def test_max_value_constraint(self):
        """최대값 3.0 초과 시 ValidationError."""
        with pytest.raises(Exception):
            ThrottleSettings(shedding_compensation_factor=3.5)

    def test_valid_boundary_values(self):
        """경계값 1.0, 3.0 허용."""
        settings_min = ThrottleSettings(shedding_compensation_factor=1.0)
        assert settings_min.shedding_compensation_factor == 1.0

        settings_max = ThrottleSettings(shedding_compensation_factor=3.0)
        assert settings_max.shedding_compensation_factor == 3.0

    def test_custom_value(self):
        """커스텀 값 설정."""
        settings = ThrottleSettings(shedding_compensation_factor=2.0)
        assert settings.shedding_compensation_factor == 2.0
