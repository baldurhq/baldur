"""
EmergencyHealthPenalty 단위 테스트.

Emergency 상태에 따른 Health Score 감점 계산 테스트.
"""

from unittest.mock import MagicMock

import pytest

from baldur.models.emergency import EmergencyLevel, EmergencyScope
from baldur.services.regional_emergency.health_penalty import (
    GLOBAL_STRICT_PENALTY,
    LEVEL_1_PENALTY,
    LEVEL_2_PENALTY,
    REGIONAL_STRICT_PENALTY,
    EmergencyHealthPenalty,
    PenaltyBreakdown,
    get_emergency_health_penalty,
    reset_emergency_health_penalty,
)


@pytest.fixture(autouse=True)
def reset_singleton():
    """테스트 간 싱글톤 초기화."""
    reset_emergency_health_penalty()
    yield
    reset_emergency_health_penalty()


@pytest.fixture
def mock_tracker():
    """Mock NamespacedEmergencyTracker."""
    return MagicMock()


def create_mock_state(
    namespace: str = "seoul",
    scope: EmergencyScope = EmergencyScope.REGIONAL,
    governance_mode: str = "NORMAL",
    emergency_level: EmergencyLevel = EmergencyLevel.NORMAL,
    activated_by: str = None,
    activated_at: str = None,
) -> MagicMock:
    """ScopedEmergencyState Mock 생성."""
    mock = MagicMock()
    mock.namespace = namespace
    mock.scope = scope
    mock.governance_mode = governance_mode
    mock.emergency_level = emergency_level
    mock.activated_by = activated_by
    mock.activated_at = activated_at
    mock.is_active.return_value = emergency_level != EmergencyLevel.NORMAL
    return mock


class TestPenaltyBreakdown:
    """PenaltyBreakdown 데이터 클래스 테스트."""

    def test_default_values(self):
        """기본값 확인."""
        breakdown = PenaltyBreakdown()

        assert breakdown.penalty == 0.0
        assert breakdown.reason is None
        assert breakdown.scope is None
        assert breakdown.emergency_level == "NORMAL"
        assert breakdown.governance_mode == "NORMAL"

    def test_to_dict(self):
        """딕셔너리 변환."""
        breakdown = PenaltyBreakdown(
            penalty=20.0,
            reason="Test reason",
            scope="regional",
            emergency_level="LEVEL_3",
            governance_mode="STRICT",
            namespace="seoul",
        )

        result = breakdown.to_dict()

        assert result["penalty"] == 20.0
        assert result["reason"] == "Test reason"
        assert result["scope"] == "regional"
        assert result["emergency_level"] == "LEVEL_3"
        assert result["governance_mode"] == "STRICT"
        assert result["namespace"] == "seoul"
        assert "calculated_at" in result


class TestEmergencyHealthPenalty:
    """EmergencyHealthPenalty 기본 테스트."""

    def test_no_penalty_when_not_active(self, mock_tracker):
        """Emergency 비활성화 시 감점 없음."""
        mock_tracker.get_effective_state.return_value = create_mock_state(
            namespace="seoul",
            scope=EmergencyScope.REGIONAL,
            governance_mode="NORMAL",
            emergency_level=EmergencyLevel.NORMAL,
        )

        penalty = EmergencyHealthPenalty(tracker=mock_tracker)
        result = penalty.calculate_penalty("seoul")

        assert result == 0.0

    def test_regional_strict_penalty(self, mock_tracker):
        """Regional STRICT 감점 확인."""
        mock_tracker.get_effective_state.return_value = create_mock_state(
            namespace="seoul",
            scope=EmergencyScope.REGIONAL,
            governance_mode="STRICT",
            emergency_level=EmergencyLevel.LEVEL_3,
        )

        penalty = EmergencyHealthPenalty(tracker=mock_tracker)
        result = penalty.calculate_penalty("seoul")

        assert result == REGIONAL_STRICT_PENALTY  # 20점

    def test_global_strict_penalty(self, mock_tracker):
        """Global STRICT 감점 확인."""
        mock_tracker.get_effective_state.return_value = create_mock_state(
            namespace="global",
            scope=EmergencyScope.GLOBAL,
            governance_mode="STRICT",
            emergency_level=EmergencyLevel.LEVEL_3,
        )

        penalty = EmergencyHealthPenalty(tracker=mock_tracker)
        result = penalty.calculate_penalty("global")

        assert result == GLOBAL_STRICT_PENALTY  # 30점

    def test_level_1_penalty(self, mock_tracker):
        """LEVEL_1 (비STRICT) 감점 확인."""
        mock_tracker.get_effective_state.return_value = create_mock_state(
            namespace="seoul",
            scope=EmergencyScope.REGIONAL,
            governance_mode="NORMAL",
            emergency_level=EmergencyLevel.LEVEL_1,
        )

        penalty = EmergencyHealthPenalty(tracker=mock_tracker)
        result = penalty.calculate_penalty("seoul")

        assert result == LEVEL_1_PENALTY  # 5점

    def test_level_2_non_strict_penalty(self, mock_tracker):
        """LEVEL_2 (비STRICT) 감점 확인."""
        mock_tracker.get_effective_state.return_value = create_mock_state(
            namespace="seoul",
            scope=EmergencyScope.REGIONAL,
            governance_mode="NORMAL",  # STRICT 아님
            emergency_level=EmergencyLevel.LEVEL_2,
        )

        penalty = EmergencyHealthPenalty(tracker=mock_tracker)
        result = penalty.calculate_penalty("seoul")

        assert result == LEVEL_2_PENALTY  # 10점


class TestHealthScoreAdjustment:
    """Health Score 조정 테스트."""

    def test_apply_penalty_to_base_score(self, mock_tracker):
        """기본 점수에 감점 적용."""
        mock_tracker.get_effective_state.return_value = create_mock_state(
            namespace="seoul",
            scope=EmergencyScope.REGIONAL,
            governance_mode="STRICT",
            emergency_level=EmergencyLevel.LEVEL_3,
        )

        penalty = EmergencyHealthPenalty(tracker=mock_tracker)
        result = penalty.get_health_score_with_emergency(
            base_score=95.0, namespace="seoul"
        )

        assert result == 75.0  # 95 - 20

    def test_clamp_minimum_to_zero(self, mock_tracker):
        """최소값 0으로 클램프."""
        mock_tracker.get_effective_state.return_value = create_mock_state(
            namespace="global",
            scope=EmergencyScope.GLOBAL,
            governance_mode="STRICT",
            emergency_level=EmergencyLevel.LEVEL_3,
        )

        penalty = EmergencyHealthPenalty(tracker=mock_tracker)
        result = penalty.get_health_score_with_emergency(
            base_score=20.0,  # 20 - 30 = -10
            namespace="global",
        )

        assert result == 0.0  # 최소값

    def test_clamp_maximum_to_100(self, mock_tracker):
        """최대값 100으로 클램프."""
        mock_tracker.get_effective_state.return_value = create_mock_state(
            namespace="seoul",
            scope=EmergencyScope.REGIONAL,
            governance_mode="NORMAL",
            emergency_level=EmergencyLevel.NORMAL,
        )

        penalty = EmergencyHealthPenalty(tracker=mock_tracker)
        result = penalty.get_health_score_with_emergency(
            base_score=150.0,  # 비정상 입력
            namespace="seoul",
        )

        assert result == 100.0  # 최대값


class TestPenaltyBreakdownMethod:
    """get_penalty_breakdown 메서드 테스트."""

    def test_no_penalty_breakdown(self, mock_tracker):
        """감점 없을 때 breakdown."""
        mock_tracker.get_effective_state.return_value = create_mock_state(
            namespace="seoul",
            scope=EmergencyScope.REGIONAL,
            governance_mode="NORMAL",
            emergency_level=EmergencyLevel.NORMAL,
        )

        penalty = EmergencyHealthPenalty(tracker=mock_tracker)
        breakdown = penalty.get_penalty_breakdown("seoul")

        assert breakdown.penalty == 0.0
        assert breakdown.reason is None
        assert breakdown.scope is None

    def test_penalty_breakdown_with_active_emergency(self, mock_tracker):
        """Emergency 활성화 시 breakdown."""
        mock_tracker.get_effective_state.return_value = create_mock_state(
            namespace="seoul",
            scope=EmergencyScope.REGIONAL,
            governance_mode="STRICT",
            emergency_level=EmergencyLevel.LEVEL_3,
            activated_by="admin@company.com",
            activated_at="2026-01-22T10:00:00+00:00",
        )

        penalty = EmergencyHealthPenalty(tracker=mock_tracker)
        breakdown = penalty.get_penalty_breakdown("seoul")

        assert breakdown.penalty == REGIONAL_STRICT_PENALTY
        assert "STRICT" in breakdown.reason
        assert breakdown.scope == "regional"
        assert breakdown.emergency_level == "LEVEL_3"
        assert breakdown.activated_by == "admin@company.com"
        assert breakdown.activated_at == "2026-01-22T10:00:00+00:00"


class TestCacheManagement:
    """캐시 관리 테스트."""

    def test_cache_hit_reduces_tracker_calls(self, mock_tracker):
        """캐시 히트 시 tracker 호출 감소."""
        mock_tracker.get_effective_state.return_value = create_mock_state(
            namespace="seoul",
            scope=EmergencyScope.REGIONAL,
            governance_mode="STRICT",
            emergency_level=EmergencyLevel.LEVEL_3,
        )

        penalty = EmergencyHealthPenalty(tracker=mock_tracker)

        # 첫 번째 호출
        penalty.calculate_penalty("seoul")
        # 두 번째 호출 (캐시 히트)
        penalty.calculate_penalty("seoul")

        # tracker는 1번만 호출되어야 함
        assert mock_tracker.get_effective_state.call_count == 1

    def test_invalidate_cache_specific_namespace(self, mock_tracker):
        """특정 네임스페이스 캐시 무효화."""
        mock_tracker.get_effective_state.return_value = create_mock_state(
            namespace="seoul",
            scope=EmergencyScope.REGIONAL,
            governance_mode="STRICT",
            emergency_level=EmergencyLevel.LEVEL_3,
        )

        penalty = EmergencyHealthPenalty(tracker=mock_tracker)

        # 캐시 채우기
        penalty.calculate_penalty("seoul")

        # 캐시 무효화
        penalty.invalidate_cache("seoul")

        # 다시 호출
        penalty.calculate_penalty("seoul")

        # tracker는 2번 호출되어야 함
        assert mock_tracker.get_effective_state.call_count == 2

    def test_invalidate_cache_all(self, mock_tracker):
        """전체 캐시 무효화."""
        mock_tracker.get_effective_state.return_value = create_mock_state(
            namespace="seoul",
            scope=EmergencyScope.REGIONAL,
            governance_mode="STRICT",
            emergency_level=EmergencyLevel.LEVEL_3,
        )

        penalty = EmergencyHealthPenalty(tracker=mock_tracker)

        # 캐시 채우기
        penalty.calculate_penalty("seoul")
        penalty.calculate_penalty("tokyo")

        # 전체 캐시 무효화
        penalty.invalidate_cache()

        # 다시 호출
        penalty.calculate_penalty("seoul")
        penalty.calculate_penalty("tokyo")

        # tracker는 4번 호출되어야 함
        assert mock_tracker.get_effective_state.call_count == 4


class TestCustomPenaltyValues:
    """커스텀 감점 값 테스트."""

    def test_custom_regional_penalty(self, mock_tracker):
        """커스텀 Regional 감점 값."""
        mock_tracker.get_effective_state.return_value = create_mock_state(
            namespace="seoul",
            scope=EmergencyScope.REGIONAL,
            governance_mode="STRICT",
            emergency_level=EmergencyLevel.LEVEL_3,
        )

        penalty = EmergencyHealthPenalty(
            tracker=mock_tracker,
            regional_penalty=25.0,
        )
        result = penalty.calculate_penalty("seoul")

        assert result == 25.0

    def test_custom_global_penalty(self, mock_tracker):
        """커스텀 Global 감점 값."""
        mock_tracker.get_effective_state.return_value = create_mock_state(
            namespace="global",
            scope=EmergencyScope.GLOBAL,
            governance_mode="STRICT",
            emergency_level=EmergencyLevel.LEVEL_3,
        )

        penalty = EmergencyHealthPenalty(
            tracker=mock_tracker,
            global_penalty=40.0,
        )
        result = penalty.calculate_penalty("global")

        assert result == 40.0


class TestSingleton:
    """싱글톤 테스트."""

    def test_singleton_returns_same_instance(self):
        """싱글톤 동일 인스턴스 반환."""
        instance1 = get_emergency_health_penalty()
        instance2 = get_emergency_health_penalty()

        assert instance1 is instance2

    def test_reset_clears_singleton(self):
        """싱글톤 초기화."""
        instance1 = get_emergency_health_penalty()
        reset_emergency_health_penalty()
        instance2 = get_emergency_health_penalty()

        assert instance1 is not instance2
