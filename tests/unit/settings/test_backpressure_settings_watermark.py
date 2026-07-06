"""
BackpressureSettings Watermark·CPU 임계치·Retry-After 배율 단위 테스트.

테스트 항목:
- 계약: CPU 사용률 임계치 기본값
- 계약: Priority Watermark 비율 기본값
- 계약: 레벨별 Retry-After 배율 기본값
- 동작: get_priority_watermarks() 딕셔너리 반환
- 동작: get_retry_after_for_level() 레벨별 배율 적용
"""

import pytest

from baldur.settings.backpressure import (
    BackpressureLevel,
    BackpressureSettings,
    reset_backpressure_settings,
)


class TestCpuThresholdContract:
    """CPU 사용률 임계치 설계 계약값 검증."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        reset_backpressure_settings()
        yield
        reset_backpressure_settings()

    def test_cpu_high_threshold_default(self):
        """CPU high 임계치 기본값은 80.0."""
        settings = BackpressureSettings()
        assert settings.resource_cpu_high_threshold == 80.0

    def test_cpu_critical_threshold_default(self):
        """CPU critical 임계치 기본값은 90.0."""
        settings = BackpressureSettings()
        assert settings.resource_cpu_critical_threshold == 90.0


class TestWatermarkContract:
    """Priority Watermark 설계 계약값 검증."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        reset_backpressure_settings()
        yield
        reset_backpressure_settings()

    def test_watermark_critical_default(self):
        """critical watermark 기본값은 0.0."""
        settings = BackpressureSettings()
        assert settings.watermark_critical == 0.0

    def test_watermark_standard_default(self):
        """standard watermark 기본값은 0.3."""
        settings = BackpressureSettings()
        assert settings.watermark_standard == 0.3

    def test_watermark_non_essential_default(self):
        """non_essential watermark 기본값은 0.6."""
        settings = BackpressureSettings()
        assert settings.watermark_non_essential == 0.6


class TestRetryAfterMultiplierContract:
    """레벨별 Retry-After 배율 설계 계약값 검증."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        reset_backpressure_settings()
        yield
        reset_backpressure_settings()

    def test_reject_retry_after_seconds_default(self):
        """Retry-After base 기본값은 5초."""
        settings = BackpressureSettings()
        assert settings.reject_retry_after_seconds == 5

    def test_none_level_retry_after(self):
        """NONE 레벨 Retry-After는 5."""
        settings = BackpressureSettings()
        assert settings.get_retry_after_for_level(BackpressureLevel.NONE) == 5

    def test_low_level_retry_after(self):
        """LOW 레벨 Retry-After는 5."""
        settings = BackpressureSettings()
        assert settings.get_retry_after_for_level(BackpressureLevel.LOW) == 5

    def test_medium_level_retry_after(self):
        """MEDIUM 레벨 Retry-After는 10."""
        settings = BackpressureSettings()
        assert settings.get_retry_after_for_level(BackpressureLevel.MEDIUM) == 10

    def test_high_level_retry_after(self):
        """HIGH 레벨 Retry-After는 20."""
        settings = BackpressureSettings()
        assert settings.get_retry_after_for_level(BackpressureLevel.HIGH) == 20

    def test_critical_level_retry_after(self):
        """CRITICAL 레벨 Retry-After는 40."""
        settings = BackpressureSettings()
        assert settings.get_retry_after_for_level(BackpressureLevel.CRITICAL) == 40


class TestGetPriorityWatermarksBehavior:
    """get_priority_watermarks() 동작 검증."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        reset_backpressure_settings()
        yield
        reset_backpressure_settings()

    def test_returns_three_tier_keys(self):
        """3개 tier 키를 포함하는 dict를 반환한다."""
        settings = BackpressureSettings()
        wm = settings.get_priority_watermarks()
        assert set(wm.keys()) == {"critical", "standard", "non_essential"}

    def test_values_match_instance_fields(self):
        """반환값이 인스턴스 watermark 필드와 동일하다."""
        settings = BackpressureSettings()
        wm = settings.get_priority_watermarks()
        assert wm["critical"] == settings.watermark_critical
        assert wm["standard"] == settings.watermark_standard
        assert wm["non_essential"] == settings.watermark_non_essential

    def test_custom_values_reflected(self):
        """커스텀 watermark 값이 반환값에 반영된다."""
        settings = BackpressureSettings(
            watermark_critical=0.1,
            watermark_standard=0.4,
            watermark_non_essential=0.8,
        )
        wm = settings.get_priority_watermarks()
        assert wm["critical"] == 0.1
        assert wm["standard"] == 0.4
        assert wm["non_essential"] == 0.8


class TestGetRetryAfterForLevelBehavior:
    """get_retry_after_for_level() 동작 검증."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        reset_backpressure_settings()
        yield
        reset_backpressure_settings()

    def test_none_level_equals_base(self):
        """NONE 레벨은 base 값을 그대로 반환한다."""
        settings = BackpressureSettings()
        result = settings.get_retry_after_for_level(BackpressureLevel.NONE)
        assert result == settings.reject_retry_after_seconds

    def test_critical_greater_than_high(self):
        """CRITICAL 레벨 Retry-After는 HIGH보다 크다."""
        settings = BackpressureSettings()
        critical = settings.get_retry_after_for_level(BackpressureLevel.CRITICAL)
        high = settings.get_retry_after_for_level(BackpressureLevel.HIGH)
        assert critical > high

    def test_monotonically_increasing(self):
        """NONE ≤ LOW ≤ MEDIUM ≤ HIGH ≤ CRITICAL 순으로 증가한다."""
        settings = BackpressureSettings()
        levels = [
            BackpressureLevel.NONE,
            BackpressureLevel.LOW,
            BackpressureLevel.MEDIUM,
            BackpressureLevel.HIGH,
            BackpressureLevel.CRITICAL,
        ]
        values = [settings.get_retry_after_for_level(lv) for lv in levels]
        for i in range(1, len(values)):
            assert values[i] >= values[i - 1]

    def test_custom_base_scales_all_levels(self):
        """base 값을 2배로 변경하면 모든 레벨 결과값도 2배가 된다."""
        s1 = BackpressureSettings(reject_retry_after_seconds=5)
        s2 = BackpressureSettings(reject_retry_after_seconds=10)
        for level in BackpressureLevel:
            v1 = s1.get_retry_after_for_level(level)
            v2 = s2.get_retry_after_for_level(level)
            assert v2 == v1 * 2
