"""
AdaptiveThrottle 429 이벤트 연동 단위 테스트.

테스트 대상:
- 429 이벤트 수신 시 limit 감소
- 연속 429 점진적 감소 (20%/40%/50%)
- min_limit 하한 보호
- Conservative Limit (Min-Winner 정책)
- Priority-aware CRITICAL 티어 보호
- COOLDOWN_END 복구
- BaldurEvent 객체 지원
- SLA Warning 발행
- is_rate_limited_for_key 상태 확인
- check() 메서드 티어별 limit 적용
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

from tests.unit.rate_limit.conftest import (
    DEFAULT_INITIAL_LIMIT,
    DEFAULT_MIN_LIMIT,
    REDUCTION_RATIO_1ST,
    REDUCTION_RATIO_2ND,
    REDUCTION_RATIO_3RD,
    SLA_WARNING_THRESHOLD,
    compute_progressive_limits,
    make_429_event,
    make_cooldown_end_event,
)

# =============================================================================
# 429 Limit 감소 테스트
# =============================================================================


class TestAdaptiveThrottle429Integration:
    """AdaptiveThrottle 429 이벤트 연동 테스트."""

    def test_handle_rate_limit_429_reduces_limit(self, adaptive_throttle):
        """429 이벤트 수신 시 limit 감소 확인."""
        initial = adaptive_throttle.current_limit
        assert initial == DEFAULT_INITIAL_LIMIT

        adaptive_throttle._handle_rate_limit_429(make_429_event(consecutive_429s=1))

        expected = int(initial * REDUCTION_RATIO_1ST)
        assert adaptive_throttle.current_limit == expected

    def test_consecutive_429_progressive_reduction(self, adaptive_throttle):
        """연속 429 시 점진적 감소 확인."""
        limits = [adaptive_throttle.current_limit]

        for i in range(1, 4):
            adaptive_throttle._handle_rate_limit_429(
                make_429_event(consecutive_429s=i, cooldown_seconds=i * 10)
            )
            limits.append(adaptive_throttle.current_limit)

        expected = compute_progressive_limits()
        assert limits == expected

    @pytest.mark.parametrize(
        ("consecutive", "ratio"),
        [
            (1, REDUCTION_RATIO_1ST),
            (2, REDUCTION_RATIO_2ND),
            (3, REDUCTION_RATIO_3RD),
            (5, REDUCTION_RATIO_3RD),
        ],
        ids=["1st-20%", "2nd-40%", "3rd-50%", "5th-capped-50%"],
    )
    def test_reduction_ratio_by_consecutive_count(
        self, adaptive_throttle, consecutive, ratio
    ):
        """연속 횟수별 감소 비율 개별 검증."""
        initial = adaptive_throttle.current_limit
        adaptive_throttle._handle_rate_limit_429(
            make_429_event(consecutive_429s=consecutive)
        )
        # 누적이 아닌 단일 적용이므로 initial 기준으로 계산
        expected = max(int(initial * ratio), DEFAULT_MIN_LIMIT)
        assert adaptive_throttle.current_limit == expected

    def test_429_respects_min_limit(self, adaptive_throttle):
        """429 감소가 min_limit 이상 유지."""
        for i in range(1, 10):
            adaptive_throttle._handle_rate_limit_429(make_429_event(consecutive_429s=i))

        assert adaptive_throttle.current_limit >= DEFAULT_MIN_LIMIT


# =============================================================================
# Conservative Limit (Min-Winner) 정책 테스트
# =============================================================================


class TestAdaptiveThrottleConservativeLimit:
    """AdaptiveThrottle Conservative Limit (Min-Winner) 정책 테스트."""

    @pytest.mark.parametrize(
        ("rtt_limit", "limit_429", "expected"),
        [
            (80, 60, 60),
            (50, 100, 50),
        ],
        ids=["429-lower-wins", "rtt-lower-wins"],
    )
    def test_conservative_limit_selects_min(
        self, adaptive_throttle, rtt_limit, limit_429, expected
    ):
        """RTT와 429 limit 중 낮은 값 선택."""
        adaptive_throttle._rtt_suggested_limit = rtt_limit
        adaptive_throttle._429_suggested_limit = limit_429

        assert adaptive_throttle.conservative_limit == expected

    def test_conservative_disabled_returns_current_limit(self, adaptive_throttle):
        """conservative_enabled=False 시 현재 limit 그대로 반환."""
        adaptive_throttle._conservative_enabled = False
        adaptive_throttle._rtt_suggested_limit = 50
        adaptive_throttle._429_suggested_limit = 30

        assert adaptive_throttle.conservative_limit == adaptive_throttle._current_limit


# =============================================================================
# Priority-aware CRITICAL 보호 테스트
# =============================================================================


class TestAdaptiveThrottlePriorityProtection:
    """AdaptiveThrottle Priority-aware CRITICAL 보호 테스트."""

    def test_critical_tier_uses_pre_429_limit_during_reduction(self, adaptive_throttle):
        """429 감소 상태에서 CRITICAL 티어는 이전 limit 사용."""
        adaptive_throttle._handle_rate_limit_429(make_429_event(consecutive_429s=3))

        reduced_limit = adaptive_throttle.current_limit
        assert reduced_limit < DEFAULT_INITIAL_LIMIT
        assert adaptive_throttle._429_reduction_active is True
        assert adaptive_throttle._limit_before_429 == DEFAULT_INITIAL_LIMIT


class TestAdaptiveThrottleCheckPriorityProtection:
    """check() 메서드 CRITICAL 티어 실제 limit 적용 테스트."""

    def test_check_critical_tier_allows_more_than_reduced_limit(
        self, adaptive_throttle
    ):
        """CRITICAL 티어는 429 감소 전 limit 기준으로 체크."""
        adaptive_throttle._handle_rate_limit_429(make_429_event(consecutive_429s=3))

        assert adaptive_throttle._429_reduction_active is True
        reduced = adaptive_throttle.current_limit
        assert reduced < DEFAULT_INITIAL_LIMIT

        adaptive_throttle.check("req_standard", tier_id="standard")
        adaptive_throttle.check("req_critical", tier_id="critical")

        assert adaptive_throttle._current_limit == reduced  # 복원됨

    def test_check_standard_tier_not_protected(self, adaptive_throttle):
        """standard 티어는 감소된 limit 그대로 적용."""
        reduced = 50
        adaptive_throttle._429_reduction_active = True
        adaptive_throttle._limit_before_429 = DEFAULT_INITIAL_LIMIT
        adaptive_throttle._current_limit = reduced

        adaptive_throttle.check("req_test", tier_id="standard")
        assert adaptive_throttle._current_limit == reduced


# =============================================================================
# COOLDOWN_END 복구 테스트
# =============================================================================


class TestAdaptiveThrottleCooldownEndRecovery:
    """AdaptiveThrottle COOLDOWN_END 이벤트 복구 테스트."""

    def test_handle_cooldown_end_clears_429_state(self, adaptive_throttle):
        """COOLDOWN_END 시 429 상태 초기화."""
        adaptive_throttle._handle_rate_limit_429(
            make_429_event(key="test_api", consecutive_429s=2)
        )

        assert "test_api" in adaptive_throttle._rate_limit_keys
        assert adaptive_throttle._429_reduction_active is True

        adaptive_throttle._handle_cooldown_end(make_cooldown_end_event(key="test_api"))

        assert "test_api" not in adaptive_throttle._rate_limit_keys
        assert adaptive_throttle._429_reduction_active is False

    def test_cooldown_end_starts_recovery_dampening(self, adaptive_throttle):
        """COOLDOWN_END 시 start_recovery_dampening() 호출."""
        adaptive_throttle._handle_rate_limit_429(
            make_429_event(key="test_api", consecutive_429s=2)
        )

        with patch.object(
            adaptive_throttle, "start_recovery_dampening"
        ) as mock_recovery:
            adaptive_throttle._handle_cooldown_end(
                make_cooldown_end_event(key="test_api")
            )
            mock_recovery.assert_called_once()

    def test_cooldown_end_handles_event_object(self, adaptive_throttle):
        """COOLDOWN_END에서 BaldurEvent 객체 처리."""
        adaptive_throttle._rate_limit_keys["test_api"] = time.time() + 10
        adaptive_throttle._429_reduction_active = True

        event = MagicMock()
        event.data = make_cooldown_end_event(key="test_api")

        with patch.object(adaptive_throttle, "start_recovery_dampening"):
            adaptive_throttle._handle_cooldown_end(event)

        assert "test_api" not in adaptive_throttle._rate_limit_keys
        assert adaptive_throttle._429_reduction_active is False


# =============================================================================
# BaldurEvent 객체 지원 테스트
# =============================================================================


class TestAdaptiveThrottle429BaldurEvent:
    """AdaptiveThrottle 429 핸들러 - BaldurEvent 객체 지원 테스트."""

    def test_handles_dict_event_data(self, adaptive_throttle):
        """dict 형태 이벤트 데이터 처리."""
        adaptive_throttle._handle_rate_limit_429(make_429_event(consecutive_429s=1))

        expected = int(DEFAULT_INITIAL_LIMIT * REDUCTION_RATIO_1ST)
        assert adaptive_throttle.current_limit == expected

    def test_handles_object_with_data_attribute(self, adaptive_throttle):
        """BaldurEvent 객체 (event.data) 형태 처리."""
        event = MagicMock()
        event.data = make_429_event(consecutive_429s=1)

        adaptive_throttle._handle_rate_limit_429(event)

        expected = int(DEFAULT_INITIAL_LIMIT * REDUCTION_RATIO_1ST)
        assert adaptive_throttle.current_limit == expected


# =============================================================================
# SLA Warning 발행 테스트
# =============================================================================


class TestAdaptiveThrottle429EventEmission:
    """AdaptiveThrottle 429 핸들러 이벤트 발행 테스트."""

    @staticmethod
    def _make_mock_get_bus_safe(emitted_events: list):
        """_get_event_bus_safe mock factory."""

        def mock_get_bus_safe():
            bus = MagicMock()

            def capture_emit(**kwargs):
                emitted_events.append(kwargs)
                return 1

            bus.emit = capture_emit
            return bus

        return mock_get_bus_safe

    def test_emits_sla_warning_on_threshold_consecutive(self, adaptive_throttle):
        """연속 N회 이상 429 시 THROTTLE_SLA_WARNING 발행."""
        emitted_events: list[dict] = []

        with patch(
            "baldur_pro.services.throttle.adaptive._helpers._get_event_bus_safe",
            self._make_mock_get_bus_safe(emitted_events),
        ):
            adaptive_throttle._handle_rate_limit_429(
                make_429_event(consecutive_429s=SLA_WARNING_THRESHOLD)
            )

        event_types = [str(e.get("event_type", "")) for e in emitted_events]
        assert any("SLA_WARNING" in et for et in event_types), (
            f"SLA_WARNING not found in {event_types}"
        )
        assert any("LIMIT_CHANGED" in et for et in event_types), (
            f"LIMIT_CHANGED not found in {event_types}"
        )

    def test_no_sla_warning_on_1_consecutive(self, adaptive_throttle):
        """단일 429 시 SLA Warning 미발행."""
        emitted_events: list[dict] = []

        with patch(
            "baldur_pro.services.throttle.adaptive._helpers._get_event_bus_safe",
            self._make_mock_get_bus_safe(emitted_events),
        ):
            adaptive_throttle._handle_rate_limit_429(make_429_event(consecutive_429s=1))

        event_types = [str(e.get("event_type", "")) for e in emitted_events]
        assert not any("SLA_WARNING" in et for et in event_types)


# =============================================================================
# is_rate_limited_for_key 상태 확인 테스트
# =============================================================================


class TestAdaptiveThrottleIsRateLimited:
    """is_rate_limited_for_key() 테스트."""

    def test_returns_true_during_cooldown(self, adaptive_throttle):
        """cooldown 중이면 True 반환."""
        adaptive_throttle._rate_limit_keys["test_api"] = time.time() + 100
        assert adaptive_throttle.is_rate_limited_for_key("test_api") is True

    def test_returns_false_after_cooldown(self, adaptive_throttle):
        """cooldown 종료 후 False 반환."""
        adaptive_throttle._rate_limit_keys["test_api"] = time.time() - 5
        assert adaptive_throttle.is_rate_limited_for_key("test_api") is False

    def test_returns_false_for_unknown_key(self, adaptive_throttle):
        """등록되지 않은 key는 False 반환."""
        assert adaptive_throttle.is_rate_limited_for_key("unknown_api") is False
