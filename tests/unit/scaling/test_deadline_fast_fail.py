"""
단위 테스트 — Dynamic Fast-Fail 예상 처리시간 산출.

테스트 항목:
- RTT 데이터 기반 예상 처리시간 산출 (smoothed_rtt × safety_margin)
- Cold Start 시 Tier별 기본값 반환
- gradient 양수일 때 safety_margin 증가
- should_fast_fail과 estimated_processing_ms 통합
- ImportError 발생 시 Tier별 기본값 반환
"""

from __future__ import annotations

import pytest

pytest.importorskip("baldur_pro")

pytestmark = pytest.mark.requires_pro


from unittest.mock import patch

import pytest

from baldur.scaling.deadline_context import (
    DEFAULT_ESTIMATED_MS_CRITICAL,
    DEFAULT_ESTIMATED_MS_NON_ESSENTIAL,
    DEFAULT_ESTIMATED_MS_STANDARD,
    _request_deadline,
    get_estimated_processing_ms,
    get_tier_default_estimated_ms,
    set_deadline,
    should_fast_fail,
)
from baldur_pro.services.throttle.gradient import (
    get_gradient_calculator,
    reset_gradient_calculators,
)


@pytest.fixture(autouse=True)
def _reset_state():
    """각 테스트 전후로 Calculator 레지스트리와 deadline ContextVar를 초기화."""
    reset_gradient_calculators()
    _request_deadline.set(None)
    yield
    reset_gradient_calculators()
    _request_deadline.set(None)


class TestTierDefaultEstimatedMsContract:
    """Tier별 Cold Start 기본 예상 처리시간 계약값 검증."""

    def test_critical_default_50ms(self):
        """critical tier 기본값은 50ms이다."""
        assert DEFAULT_ESTIMATED_MS_CRITICAL == 50.0

    def test_standard_default_200ms(self):
        """standard tier 기본값은 200ms이다."""
        assert DEFAULT_ESTIMATED_MS_STANDARD == 200.0

    def test_non_essential_default_500ms(self):
        """non_essential tier 기본값은 500ms이다."""
        assert DEFAULT_ESTIMATED_MS_NON_ESSENTIAL == 500.0


class TestTierDefaultEstimatedMsBehavior:
    """Tier별 Cold Start 기본 예상 처리시간 동작 검증."""

    def test_critical_tier_returns_critical_default(self):
        """critical tier는 critical 기본값을 반환한다."""
        result = get_tier_default_estimated_ms("critical")
        assert result == DEFAULT_ESTIMATED_MS_CRITICAL

    def test_standard_tier_returns_standard_default(self):
        """standard tier는 standard 기본값을 반환한다."""
        result = get_tier_default_estimated_ms("standard")
        assert result == DEFAULT_ESTIMATED_MS_STANDARD

    def test_non_essential_tier_returns_non_essential_default(self):
        """non_essential tier는 non_essential 기본값을 반환한다."""
        result = get_tier_default_estimated_ms("non_essential")
        assert result == DEFAULT_ESTIMATED_MS_NON_ESSENTIAL

    def test_unknown_tier_returns_standard_default(self):
        """알 수 없는 tier는 standard 기본값을 반환한다."""
        result = get_tier_default_estimated_ms("unknown_tier")
        assert result == DEFAULT_ESTIMATED_MS_STANDARD

    def test_default_parameter_returns_standard(self):
        """매개변수 생략 시 standard 기본값을 반환한다."""
        result = get_tier_default_estimated_ms()
        assert result == DEFAULT_ESTIMATED_MS_STANDARD


class TestGetEstimatedProcessingMsBehavior:
    """GradientCalculator 기반 예상 처리시간 동작 검증."""

    def test_with_rtt_data_applies_safety_margin(self):
        """RTT 데이터가 있으면 smoothed_rtt × safety_margin을 반환한다."""
        calc = get_gradient_calculator("test_estimated")
        # 2개 샘플을 추가하여 previous_smoothed_rtt도 설정
        calc.add_sample(200.0)
        calc.add_sample(200.0)

        result = get_estimated_processing_ms(
            calculator_name="test_estimated",
            safety_margin=1.5,
            tier_id="standard",
        )

        # smoothed_rtt ≈ 200ms, safety_margin=1.5 → 약 300ms
        assert result > 0
        # 정확한 값은 EMA에 따라 다를 수 있으나, 200 * 1.5 = 300 근처
        assert 200.0 < result < 400.0

    def test_cold_start_returns_tier_default_critical(self):
        """RTT 데이터 없음(Cold Start) → critical tier 기본값 반환."""
        result = get_estimated_processing_ms(
            calculator_name="test_cold_critical",
            tier_id="critical",
        )
        assert result == DEFAULT_ESTIMATED_MS_CRITICAL

    def test_cold_start_returns_tier_default_standard(self):
        """RTT 데이터 없음(Cold Start) → standard tier 기본값 반환."""
        result = get_estimated_processing_ms(
            calculator_name="test_cold_standard",
            tier_id="standard",
        )
        assert result == DEFAULT_ESTIMATED_MS_STANDARD

    def test_cold_start_returns_tier_default_non_essential(self):
        """RTT 데이터 없음(Cold Start) → non_essential tier 기본값 반환."""
        result = get_estimated_processing_ms(
            calculator_name="test_cold_ne",
            tier_id="non_essential",
        )
        assert result == DEFAULT_ESTIMATED_MS_NON_ESSENTIAL

    def test_positive_gradient_increases_margin(self):
        """gradient가 양수(RTT 증가 추세)이면 safety_margin이 증가한다."""
        calc = get_gradient_calculator("test_gradient_up")
        # RTT가 증가하는 패턴
        calc.add_sample(100.0)
        calc.add_sample(200.0)  # gradient > 0

        result_increasing = get_estimated_processing_ms(
            calculator_name="test_gradient_up",
            safety_margin=1.5,
            tier_id="standard",
        )

        # gradient > 0.1 이면 effective_margin = 1.5 * (1 + gradient) > 1.5
        # 따라서 단순 rtt * 1.5 보다 크다
        rtt = calc.get_current_rtt()
        base_estimate = rtt * 1.5
        assert result_increasing > base_estimate

    def test_stable_gradient_keeps_margin(self):
        """gradient가 작으면(안정 추세) safety_margin이 변하지 않는다."""
        calc = get_gradient_calculator("test_gradient_stable")
        # 동일한 RTT를 반복하여 gradient ≈ 0
        for _ in range(5):
            calc.add_sample(100.0)

        result = get_estimated_processing_ms(
            calculator_name="test_gradient_stable",
            safety_margin=1.5,
            tier_id="standard",
        )

        rtt = calc.get_current_rtt()
        # gradient ≈ 0 이므로 result ≈ rtt * 1.5
        assert result == pytest.approx(rtt * 1.5, rel=0.1)

    def test_import_error_returns_tier_default(self):
        """gradient 모듈 ImportError 시 Tier별 기본값을 반환한다."""
        original_import = (
            __builtins__.__import__
            if hasattr(__builtins__, "__import__")
            else __import__
        )

        def fail_gradient_import(name, *args, **kwargs):
            if "gradient" in name:
                raise ImportError("mocked gradient module not found")
            return original_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=fail_gradient_import):
            result = get_estimated_processing_ms(
                calculator_name="test_import_error",
                tier_id="critical",
            )

        assert result == DEFAULT_ESTIMATED_MS_CRITICAL

    def test_should_fast_fail_with_estimated_ms(self):
        """remaining < estimated 이면 should_fast_fail이 True를 반환한다."""
        set_deadline(400.0)  # 400ms 남음 (네트워크 buffer 50ms 차감 → 실질 350ms)

        calc = get_gradient_calculator("test_fast_fail")
        calc.add_sample(300.0)
        calc.add_sample(300.0)

        estimated = get_estimated_processing_ms(
            calculator_name="test_fast_fail",
            safety_margin=1.5,
            tier_id="standard",
        )

        # estimated ≈ 300 * 1.5 = 450ms
        # remaining ≈ 350ms (400 - 50 buffer)
        # 450 > 350 → should_fast_fail = True
        assert estimated > 350.0
        assert should_fast_fail(estimated) is True

    def test_should_fast_fail_with_enough_time(self):
        """remaining > estimated 이면 should_fast_fail이 False를 반환한다."""
        set_deadline(5000.0)  # 5초 → 충분

        calc = get_gradient_calculator("test_no_fail")
        calc.add_sample(100.0)
        calc.add_sample(100.0)

        estimated = get_estimated_processing_ms(
            calculator_name="test_no_fail",
            safety_margin=1.5,
            tier_id="standard",
        )

        # estimated ≈ 150ms, remaining ≈ 4950ms
        assert should_fast_fail(estimated) is False

    def test_no_deadline_no_fast_fail(self):
        """deadline 미설정 시 should_fast_fail은 항상 False."""
        estimated = get_estimated_processing_ms(
            calculator_name="test_no_deadline",
            tier_id="standard",
        )
        assert should_fast_fail(estimated) is False

    def test_return_type_always_float(self):
        """get_estimated_processing_ms는 항상 float를 반환한다 (None 아님)."""
        # Cold Start
        result = get_estimated_processing_ms(
            calculator_name="test_always_float",
            tier_id="standard",
        )
        assert isinstance(result, float)

        # RTT 데이터 있음
        calc = get_gradient_calculator("test_always_float_data")
        calc.add_sample(150.0)
        result = get_estimated_processing_ms(
            calculator_name="test_always_float_data",
            tier_id="standard",
        )
        assert isinstance(result, float)
