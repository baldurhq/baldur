"""
RateLimitThrottleIntegrationSettings 단위 테스트.

테스트 대상:
- 기본 설정 값 확인
- 감소 비율 설정
- 커스텀 설정 적용
- get_reduction_ratio() 연속 횟수별 비율
- Pydantic 검증 (범위 초과 거부)
"""

from __future__ import annotations

import pytest

from tests.unit.rate_limit.conftest import (
    DEFAULT_DEBOUNCE_WINDOW,
    REDUCTION_RATIO_1ST,
    REDUCTION_RATIO_2ND,
    REDUCTION_RATIO_3RD,
)

# =============================================================================
# Settings 테스트
# =============================================================================


class TestRateLimitThrottleIntegrationSettings:
    """RateLimitThrottleIntegrationSettings 설정 테스트."""

    def test_default_settings(self):
        """Verify default setting values."""
        from baldur.settings.rate_limit_throttle_integration import (
            RateLimitThrottleIntegrationSettings,
        )

        settings = RateLimitThrottleIntegrationSettings()

        assert settings.enabled is True
        assert settings.debounce_window_seconds == DEFAULT_DEBOUNCE_WINDOW
        assert settings.recovery_strategy == "gradual"

    def test_reduction_ratios(self):
        """429 감소 비율 설정 확인."""
        from baldur.settings.rate_limit_throttle_integration import (
            RateLimitThrottleIntegrationSettings,
        )

        settings = RateLimitThrottleIntegrationSettings()

        assert settings.reduction_ratio_1 == REDUCTION_RATIO_1ST
        assert settings.reduction_ratio_2 == REDUCTION_RATIO_2ND
        assert settings.reduction_ratio_3 == REDUCTION_RATIO_3RD

    def test_custom_settings(self):
        """Verify custom setting application."""
        from baldur.settings.rate_limit_throttle_integration import (
            RateLimitThrottleIntegrationSettings,
        )

        custom_debounce = 10.0

        settings = RateLimitThrottleIntegrationSettings(
            enabled=False,
            debounce_window_seconds=custom_debounce,
        )

        assert settings.enabled is False
        assert settings.debounce_window_seconds == custom_debounce

    @pytest.mark.parametrize(
        ("consecutive", "expected_ratio"),
        [
            (1, REDUCTION_RATIO_1ST),
            (2, REDUCTION_RATIO_2ND),
            (3, REDUCTION_RATIO_3RD),
            (5, REDUCTION_RATIO_3RD),
            (100, REDUCTION_RATIO_3RD),
        ],
        ids=["1st", "2nd", "3rd", "5th-capped", "100th-capped"],
    )
    def test_get_reduction_ratio_by_consecutive_count(
        self, consecutive, expected_ratio
    ):
        """get_reduction_ratio() 연속 횟수별 비율 반환."""
        from baldur.settings.rate_limit_throttle_integration import (
            RateLimitThrottleIntegrationSettings,
        )

        settings = RateLimitThrottleIntegrationSettings()
        assert settings.get_reduction_ratio(consecutive) == expected_ratio

    @pytest.mark.parametrize(
        ("field", "invalid_value"),
        [
            ("reduction_ratio_1", 0.0),  # 최소 0.1 미만
            ("reduction_ratio_2", 1.5),  # 최대 1.0 초과
        ],
        ids=["below-minimum", "above-maximum"],
    )
    def test_pydantic_validation_rejects_out_of_range(self, field, invalid_value):
        """Pydantic 검증 - 범위 초과 비율 거부."""
        from pydantic import ValidationError

        from baldur.settings.rate_limit_throttle_integration import (
            RateLimitThrottleIntegrationSettings,
        )

        with pytest.raises(ValidationError):
            RateLimitThrottleIntegrationSettings(**{field: invalid_value})
