"""
CascadeLoadShedding 단위 테스트.

Phase 5: Backpressure, Load Shedding 및 Fail-Soft 테스트.

Tests:
- Load Shedding 결정 로직
- 우선순위별 드롭 동작
- 버퍼 상태별 동작
- 메트릭 기록
- 로컬 폴백 동작

Reference:
    docs/baldur/middleware_system/76_CASCADE_EVENT_AUDIT.md
"""

from __future__ import annotations

import pytest

from baldur.audit.cascade_config import (
    AuditBackpressureConfig,
)
from baldur.audit.cascade_event import (
    CascadeEventPriority,
    get_priority_for_trigger,
)
from baldur.audit.cascade_load_shedding import (
    CascadeLoadShedding,
    LoadSheddingMetrics,
    get_cascade_load_shedding,
    reset_cascade_load_shedding,
)

# =============================================================================
# AuditBackpressureConfig Tests
# =============================================================================


class TestAuditBackpressureConfig:
    """AuditBackpressureConfig 단위 테스트."""

    def test_default_config(self):
        """기본 설정값 확인."""
        config = AuditBackpressureConfig()

        assert config.load_shedding_enabled is True
        assert config.buffer_warning_threshold == 0.7
        assert config.buffer_critical_threshold == 0.9
        assert config.max_events_per_second == 1000
        assert config.fallback_enabled is True
        assert config.metrics_enabled is True

    def test_custom_config(self):
        """사용자 정의 설정."""
        config = AuditBackpressureConfig(
            load_shedding_enabled=False,
            buffer_warning_threshold=0.5,
            buffer_critical_threshold=0.8,
            max_events_per_second=500,
        )

        assert config.load_shedding_enabled is False
        assert config.buffer_warning_threshold == 0.5
        assert config.buffer_critical_threshold == 0.8
        assert config.max_events_per_second == 500

    def test_threshold_validation(self):
        """임계치 검증 - warning >= critical 시 자동 조정."""
        config = AuditBackpressureConfig(
            buffer_warning_threshold=0.95,  # critical보다 높음
            buffer_critical_threshold=0.9,
        )

        # warning은 critical - 0.2로 자동 조정
        assert config.buffer_warning_threshold == 0.7


# =============================================================================
# CascadeEventPriority Tests
# =============================================================================


class TestCascadeEventPriority:
    """CascadeEventPriority 단위 테스트."""

    def test_priority_order(self):
        """우선순위 순서 확인."""
        assert CascadeEventPriority.LOW < CascadeEventPriority.MEDIUM
        assert CascadeEventPriority.MEDIUM < CascadeEventPriority.HIGH
        assert CascadeEventPriority.HIGH < CascadeEventPriority.CRITICAL

    def test_priority_values(self):
        """우선순위 값 확인."""
        assert CascadeEventPriority.LOW == 0
        assert CascadeEventPriority.MEDIUM == 1
        assert CascadeEventPriority.HIGH == 2
        assert CascadeEventPriority.CRITICAL == 3

    def test_get_priority_for_trigger_critical(self):
        """CRITICAL 트리거 타입 우선순위."""
        assert (
            get_priority_for_trigger("EMERGENCY_LEVEL_CHANGED")
            == CascadeEventPriority.CRITICAL
        )
        assert (
            get_priority_for_trigger("MANUAL_INTERVENTION")
            == CascadeEventPriority.CRITICAL
        )
        assert (
            get_priority_for_trigger("MANUAL_ACTIVATION")
            == CascadeEventPriority.CRITICAL
        )

    def test_get_priority_for_trigger_high(self):
        """HIGH 트리거 타입 우선순위."""
        assert get_priority_for_trigger("CANARY_ROLLBACK") == CascadeEventPriority.HIGH
        assert (
            get_priority_for_trigger("GOVERNANCE_MODE_CHANGED")
            == CascadeEventPriority.HIGH
        )

    def test_get_priority_for_trigger_low(self):
        """LOW 트리거 타입 우선순위."""
        assert get_priority_for_trigger("METRICS_UPDATED") == CascadeEventPriority.LOW
        assert get_priority_for_trigger("HEALTH_CHECK") == CascadeEventPriority.LOW

    def test_get_priority_for_unknown_trigger(self):
        """알 수 없는 트리거는 MEDIUM."""
        assert get_priority_for_trigger("UNKNOWN_TYPE") == CascadeEventPriority.MEDIUM
        assert get_priority_for_trigger("CUSTOM_ACTION") == CascadeEventPriority.MEDIUM


# =============================================================================
# CascadeLoadShedding Tests
# =============================================================================


class TestCascadeLoadShedding:
    """CascadeLoadShedding 단위 테스트."""

    @pytest.fixture
    def shedding(self):
        """테스트용 CascadeLoadShedding 인스턴스."""
        config = AuditBackpressureConfig(
            load_shedding_enabled=True,
            buffer_warning_threshold=0.7,
            buffer_critical_threshold=0.9,
            max_events_per_second=100,
        )
        return CascadeLoadShedding(config)

    def test_accept_when_disabled(self):
        """Load Shedding 비활성화 시 모두 수락."""
        config = AuditBackpressureConfig(load_shedding_enabled=False)
        shedding = CascadeLoadShedding(config)

        decision = shedding.should_accept(
            trigger_type="METRICS_UPDATED",
            buffer_size=9500,  # 95% - 임계치 초과
            buffer_capacity=10000,
        )

        assert decision["accepted"] is True
        assert decision["reason"] == "load_shedding_disabled"

    def test_accept_critical_always(self, shedding):
        """CRITICAL 이벤트는 항상 수락."""
        decision = shedding.should_accept(
            trigger_type="EMERGENCY_LEVEL_CHANGED",
            buffer_size=9500,  # 95% - 임계치 초과
            buffer_capacity=10000,
        )

        assert decision["accepted"] is True
        assert decision["priority"] == "CRITICAL"
        assert decision["reason"] == "critical_always_accepted"
        # 임계치 초과 시 폴백 권장
        assert decision["use_fallback"] is True

    def test_accept_in_normal_state(self, shedding):
        """정상 상태(< 70%)에서 모든 이벤트 수락."""
        decision = shedding.should_accept(
            trigger_type="METRICS_UPDATED",  # LOW priority
            buffer_size=5000,  # 50%
            buffer_capacity=10000,
        )

        assert decision["accepted"] is True
        assert decision["buffer_ratio"] == 0.5

    def test_drop_low_in_warning_state(self, shedding):
        """경고 상태(70~90%)에서 LOW 이벤트 드롭."""
        decision = shedding.should_accept(
            trigger_type="METRICS_UPDATED",  # LOW priority
            buffer_size=8000,  # 80%
            buffer_capacity=10000,
        )

        assert decision["accepted"] is False
        assert decision["priority"] == "LOW"
        assert decision["reason"] == "buffer_warning"

    def test_accept_medium_in_warning_state(self, shedding):
        """경고 상태에서 MEDIUM 이벤트는 수락."""
        decision = shedding.should_accept(
            trigger_type="BUDGET_MULTIPLIER_APPLIED",  # MEDIUM priority
            buffer_size=8000,  # 80%
            buffer_capacity=10000,
        )

        assert decision["accepted"] is True

    def test_drop_medium_in_critical_state(self, shedding):
        """임계 상태(>= 90%)에서 MEDIUM 이벤트도 드롭."""
        decision = shedding.should_accept(
            trigger_type="BUDGET_MULTIPLIER_APPLIED",  # MEDIUM priority
            buffer_size=9500,  # 95%
            buffer_capacity=10000,
        )

        assert decision["accepted"] is False
        assert decision["priority"] == "MEDIUM"
        assert decision["reason"] == "buffer_critical"

    def test_accept_high_in_critical_state(self, shedding):
        """임계 상태에서도 HIGH 이벤트는 수락."""
        decision = shedding.should_accept(
            trigger_type="CANARY_ROLLBACK",  # HIGH priority
            buffer_size=9500,  # 95%
            buffer_capacity=10000,
        )

        assert decision["accepted"] is True

    def test_fallback_recommended_for_dropped(self, shedding):
        """드롭 시 폴백 권장 여부 확인."""
        decision = shedding.should_accept(
            trigger_type="METRICS_UPDATED",
            buffer_size=8000,
            buffer_capacity=10000,
        )

        assert decision["accepted"] is False
        assert decision["use_fallback"] is True  # fallback_enabled=True

    def test_metrics_tracking(self, shedding):
        """메트릭 추적 확인."""
        # 수락
        shedding.should_accept(
            trigger_type="CANARY_ROLLBACK",
            buffer_size=5000,
            buffer_capacity=10000,
        )

        # 드롭
        shedding.should_accept(
            trigger_type="METRICS_UPDATED",
            buffer_size=8000,
            buffer_capacity=10000,
        )

        metrics = shedding.get_metrics()

        assert metrics["accepted_count"] == 1
        assert metrics["dropped_count"] == 1
        assert metrics["dropped_by_priority"]["LOW"] == 1

    def test_get_status_normal(self, shedding):
        """정상 상태 확인."""
        status = shedding.get_status(buffer_size=5000, buffer_capacity=10000)

        assert status["status"] == "NORMAL"
        assert status["shedding_level"] == "NONE"
        assert status["buffer_ratio"] == 0.5

    def test_get_status_warning(self, shedding):
        """경고 상태 확인."""
        status = shedding.get_status(buffer_size=8000, buffer_capacity=10000)

        assert status["status"] == "WARNING"
        assert status["shedding_level"] == "LOW_ONLY"

    def test_get_status_critical(self, shedding):
        """임계 상태 확인."""
        status = shedding.get_status(buffer_size=9500, buffer_capacity=10000)

        assert status["status"] == "CRITICAL"
        assert status["shedding_level"] == "MEDIUM_AND_BELOW"

    def test_reset_metrics(self, shedding):
        """메트릭 초기화."""
        # 일부 메트릭 기록
        shedding.should_accept("METRICS_UPDATED", 8000, 10000)

        assert shedding.get_metrics()["dropped_count"] > 0

        shedding.reset_metrics()

        assert shedding.get_metrics()["dropped_count"] == 0
        assert shedding.get_metrics()["accepted_count"] == 0


# =============================================================================
# Singleton Tests
# =============================================================================


class TestLoadSheddingSingleton:
    """싱글톤 패턴 테스트."""

    def setup_method(self):
        """각 테스트 전 싱글톤 리셋."""
        reset_cascade_load_shedding()

    def teardown_method(self):
        """각 테스트 후 싱글톤 리셋."""
        reset_cascade_load_shedding()

    def test_get_returns_same_instance(self):
        """동일 인스턴스 반환 확인."""
        instance1 = get_cascade_load_shedding()
        instance2 = get_cascade_load_shedding()

        assert instance1 is instance2

    def test_reset_clears_instance(self):
        """리셋 후 새 인스턴스 생성."""
        instance1 = get_cascade_load_shedding()
        reset_cascade_load_shedding()
        instance2 = get_cascade_load_shedding()

        assert instance1 is not instance2


# =============================================================================
# LoadSheddingMetrics Tests
# =============================================================================


class TestLoadSheddingMetrics:
    """LoadSheddingMetrics 단위 테스트."""

    def test_default_values(self):
        """기본값 확인."""
        metrics = LoadSheddingMetrics()

        assert metrics.accepted_count == 0
        assert metrics.dropped_count == 0
        assert metrics.fallback_count == 0

    def test_to_dict(self):
        """딕셔너리 변환."""
        metrics = LoadSheddingMetrics(
            accepted_count=100,
            dropped_count=10,
        )

        result = metrics.to_dict()

        assert result["accepted_count"] == 100
        assert result["dropped_count"] == 10
        assert result["drop_rate"] == pytest.approx(0.0909, rel=0.01)

    def test_drop_rate_calculation(self):
        """드롭률 계산."""
        metrics = LoadSheddingMetrics(
            accepted_count=80,
            dropped_count=20,
        )

        result = metrics.to_dict()

        # 20 / 100 = 0.2
        assert result["drop_rate"] == 0.2
