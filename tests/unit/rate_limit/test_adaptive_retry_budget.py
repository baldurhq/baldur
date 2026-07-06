"""
AdaptiveRetryBudget 단위 테스트.

적응형 재시도 예산 관리자 테스트.
Throttle 상태에 따른 예산 동적 조정을 검증합니다.
"""

from __future__ import annotations

import time

from baldur.services.backoff_calculator import AdaptiveRetryBudget


class TestAdaptiveRetryBudget:
    """AdaptiveRetryBudget 테스트."""

    def test_default_values(self):
        """기본값 확인."""
        budget = AdaptiveRetryBudget()

        assert budget.max_retry_ratio == 0.10  # 10%
        assert budget.current_retry_count == 0
        assert budget.current_total_count == 0
        assert budget.window_seconds == 60

    def test_should_allow_retry_when_empty(self):
        """요청이 없을 때 재시도 허용."""
        budget = AdaptiveRetryBudget()

        assert budget.should_allow_retry() is True

    def test_should_allow_retry_within_budget(self):
        """예산 내 재시도 허용."""
        budget = AdaptiveRetryBudget()

        # 100개 요청 중 5개 재시도 (5% < 10%)
        for i in range(100):
            budget.record_request(is_retry=(i < 5))

        assert budget.should_allow_retry() is True

    def test_should_not_allow_retry_over_budget(self):
        """예산 초과 시 재시도 차단."""
        budget = AdaptiveRetryBudget()

        # 100개 요청 중 15개 재시도 (15% > 10%)
        for i in range(100):
            budget.record_request(is_retry=(i < 15))

        assert budget.should_allow_retry() is False

    def test_record_request_normal(self):
        """일반 요청 기록."""
        budget = AdaptiveRetryBudget()

        budget.record_request(is_retry=False)

        assert budget.current_total_count == 1
        assert budget.current_retry_count == 0

    def test_record_request_retry(self):
        """재시도 요청 기록."""
        budget = AdaptiveRetryBudget()

        budget.record_request(is_retry=True)

        assert budget.current_total_count == 1
        assert budget.current_retry_count == 1

    def test_adjust_budget_for_normal(self):
        """정상 상태 예산 조정."""
        budget = AdaptiveRetryBudget()

        budget.adjust_budget_for_throttle_state("normal")

        assert budget.max_retry_ratio == 0.10

    def test_adjust_budget_for_sla_warning(self):
        """SLA Warning 상태 예산 조정 (7%)."""
        budget = AdaptiveRetryBudget()

        budget.adjust_budget_for_throttle_state("sla_warning")

        assert budget.max_retry_ratio == 0.07

    def test_adjust_budget_for_sla_critical(self):
        """SLA Critical 상태 예산 조정 (5%)."""
        budget = AdaptiveRetryBudget()

        budget.adjust_budget_for_throttle_state("sla_critical")

        assert budget.max_retry_ratio == 0.05

    def test_adjust_budget_for_emergency_1_2(self):
        """Emergency Level 1-2 상태 예산 조정 (3%)."""
        budget = AdaptiveRetryBudget()

        budget.adjust_budget_for_throttle_state("emergency_1_2")

        assert budget.max_retry_ratio == 0.03

        # 개별 레벨도 테스트
        budget.adjust_budget_for_throttle_state("emergency_level_1")
        assert budget.max_retry_ratio == 0.03

        budget.adjust_budget_for_throttle_state("emergency_level_2")
        assert budget.max_retry_ratio == 0.03

    def test_adjust_budget_for_emergency_3(self):
        """Emergency Level 3 상태 예산 조정 (1%)."""
        budget = AdaptiveRetryBudget()

        budget.adjust_budget_for_throttle_state("emergency_3")

        assert budget.max_retry_ratio == 0.01

        # 개별 레벨도 테스트
        budget.adjust_budget_for_throttle_state("emergency_level_3")
        assert budget.max_retry_ratio == 0.01

    def test_adjust_budget_for_full_stop(self):
        """Full Stop 상태 예산 조정 (0%)."""
        budget = AdaptiveRetryBudget()

        budget.adjust_budget_for_throttle_state("full_stop")

        assert budget.max_retry_ratio == 0.0

        # full_stop_active도 테스트
        budget.adjust_budget_for_throttle_state("full_stop_active")
        assert budget.max_retry_ratio == 0.0

    def test_adjust_budget_for_unknown(self):
        """알 수 없는 상태 시 보수적 예산 (5%)."""
        budget = AdaptiveRetryBudget()

        budget.adjust_budget_for_throttle_state("unknown_reason")

        assert budget.max_retry_ratio == 0.05

    def test_get_stats(self):
        """통계 반환."""
        budget = AdaptiveRetryBudget()

        # 100개 요청 중 5개 재시도
        for i in range(100):
            budget.record_request(is_retry=(i < 5))

        stats = budget.get_stats()

        assert stats["max_retry_ratio"] == 0.10
        assert stats["current_total_count"] == 100
        assert stats["current_retry_count"] == 5
        assert stats["current_ratio"] == 0.05
        # budget_remaining = 100 * 0.10 - 5 = 5
        assert stats["budget_remaining"] == 5

    def test_budget_remaining_zero_when_exhausted(self):
        """예산 소진 시 remaining이 0."""
        budget = AdaptiveRetryBudget()

        # 100개 요청 중 15개 재시도
        for i in range(100):
            budget.record_request(is_retry=(i < 15))

        stats = budget.get_stats()

        # budget_remaining = max(0, 100 * 0.10 - 15) = 0
        assert stats["budget_remaining"] == 0

    def test_window_reset(self):
        """윈도우 시간 초과 시 리셋."""
        budget = AdaptiveRetryBudget()
        budget.window_seconds = 1  # 테스트용으로 짧게 설정

        # 요청 기록
        for i in range(50):
            budget.record_request(is_retry=(i < 10))

        assert budget.current_total_count == 50

        # 윈도우 시간 경과 시뮬레이션
        budget._window_start = time.time() - 2  # 2초 전으로 설정

        # 다음 요청 시 리셋됨
        budget.record_request(is_retry=False)

        assert budget.current_total_count == 1
        assert budget.current_retry_count == 0

    def test_throttle_budget_ratios_constant(self):
        """THROTTLE_BUDGET_RATIOS 상수 확인."""
        budget = AdaptiveRetryBudget()

        expected_keys = {
            "normal",
            "sla_warning",
            "sla_critical",
            "emergency_level_1",
            "emergency_level_2",
            "emergency_1_2",
            "emergency_level_3",
            "emergency_3",
            "full_stop",
            "full_stop_active",
        }

        assert set(budget.THROTTLE_BUDGET_RATIOS.keys()) == expected_keys

    def test_edge_case_single_retry(self):
        """단일 재시도만 있는 경우."""
        budget = AdaptiveRetryBudget()

        budget.record_request(is_retry=True)

        # 1개 요청 중 1개 재시도 = 100% > 10%
        assert budget.should_allow_retry() is False

    def test_edge_case_zero_ratio_blocks_all(self):
        """0% 예산은 모든 재시도 차단."""
        budget = AdaptiveRetryBudget()
        budget.adjust_budget_for_throttle_state("full_stop")

        # 1개라도 재시도가 있으면 차단
        budget.record_request(is_retry=False)

        assert budget.should_allow_retry() is False
