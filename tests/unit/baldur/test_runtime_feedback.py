"""
Runtime Feedback Loop 단위 테스트

Stage 36: 실시간 메트릭 기반 자율 튜닝 시스템 테스트
패키지 내부 테스트용
"""

import pytest

pytest.importorskip("baldur_pro")

pytestmark = pytest.mark.requires_pro


from unittest.mock import Mock

from baldur.adapters.metrics.auto_tuning_adapter import MockMetricsAdapter
from baldur.core.auto_rollback_guard import (
    AutoRollbackGuard,
    GuardState,
    RollbackSeverity,
)
from baldur.core.decision_engine import (
    DecisionEngine,
)
from baldur.core.runtime_feedback import (
    FeedbackLoopState,
    RuntimeFeedbackLoop,
)
from baldur.core.safety_bounds import SafetyBounds
from baldur_pro.services.auto_tuning.adjustment_recorder import AdjustmentRecorder
from baldur_pro.services.auto_tuning.models import TuningState


class TestSafetyBounds:
    """SafetyBounds 테스트"""

    def test_default_bounds_exist(self):
        """기본 한계 설정 존재 확인"""
        bounds = SafetyBounds()
        assert bounds.get_bounds("timeout_ms") is not None
        assert bounds.get_bounds("retry_count") is not None

    def test_within_bounds_valid(self):
        """유효한 범위 내 값 확인"""
        bounds = SafetyBounds()
        assert bounds.is_within_bounds("timeout_ms", 5000)

    def test_within_bounds_invalid_too_low(self):
        """최소값 미만 거부"""
        bounds = SafetyBounds()
        assert not bounds.is_within_bounds("timeout_ms", 50)

    def test_within_bounds_invalid_too_high(self):
        """최대값 초과 거부"""
        bounds = SafetyBounds()
        assert not bounds.is_within_bounds("timeout_ms", 50000)

    def test_change_ratio_limit(self):
        """변경폭 제한 확인"""
        bounds = SafetyBounds()
        assert bounds.is_within_bounds("timeout_ms", 6000, current_value=5000)  # 20% OK
        assert not bounds.is_within_bounds(
            "timeout_ms", 8000, current_value=5000
        )  # 60% 거부

    def test_clamp_to_bounds(self):
        """범위 내 클램핑"""
        bounds = SafetyBounds()
        assert bounds.clamp_to_bounds("timeout_ms", 50) == 100
        assert bounds.clamp_to_bounds("timeout_ms", 50000) == 30000


class TestDecisionEngine:
    """DecisionEngine 테스트"""

    def setup_method(self):
        self.config_provider = Mock()
        self.config_provider.get = Mock(
            side_effect=lambda key, default=None: {
                "timeout_ms": 5000.0,
                "retry_count": 3.0,
            }.get(key, default)
        )

    def test_no_adjustment_needed(self):
        """조정 불필요 상황"""
        engine = DecisionEngine(self.config_provider)
        metrics = {"p99_latency_ms": 2000.0, "sample_count": 100}
        decisions = engine.analyze(metrics)
        timeout_decisions = [d for d in decisions if d.parameter == "timeout_ms"]
        assert len(timeout_decisions) == 0

    def test_timeout_adjustment_needed(self):
        """타임아웃 조정 필요 상황"""
        engine = DecisionEngine(self.config_provider)
        metrics = {"p99_latency_ms": 4500.0, "sample_count": 100}
        decisions = engine.analyze(metrics)
        timeout_decisions = [d for d in decisions if d.parameter == "timeout_ms"]
        assert len(timeout_decisions) == 1
        assert timeout_decisions[0].suggested_value == 6000.0

    def test_disabled_engine(self):
        """비활성화 상태"""
        engine = DecisionEngine(self.config_provider, enabled=False)
        metrics = {"p99_latency_ms": 4500.0, "sample_count": 100}
        decisions = engine.analyze(metrics)
        assert len(decisions) == 0


class TestAdjustmentRecorder:
    """AdjustmentRecorder 테스트"""

    def test_record_adjustment(self):
        """조정 기록"""
        recorder = AdjustmentRecorder()
        record = recorder.record("timeout_ms", 5000, 6000, "reason")
        assert record.parameter == "timeout_ms"
        assert record.old_value == 5000

    def test_mark_rollback(self):
        """롤백 마킹"""
        recorder = AdjustmentRecorder()
        record = recorder.record("timeout_ms", 5000, 6000, "reason")
        recorder.mark_rollback(record.record_id)
        updated = recorder.get_record(record.record_id)
        assert updated.rollback_performed is True

    def test_session_management(self):
        """세션 관리"""
        recorder = AdjustmentRecorder()
        session = recorder.start_session("Test")
        assert session.state == TuningState.ACTIVE
        recorder.record("timeout_ms", 5000, 6000, "reason")
        ended = recorder.end_session()
        assert ended.state == TuningState.COMPLETED


class TestMockMetricsAdapter:
    """MockMetricsAdapter 테스트"""

    def test_fetch_metrics(self):
        """메트릭 조회"""
        adapter = MockMetricsAdapter()
        metrics = adapter.fetch_current_metrics()
        assert "error_rate" in metrics
        assert "p99_latency_ms" in metrics

    def test_simulate_degradation(self):
        """저하 시뮬레이션"""
        adapter = MockMetricsAdapter()
        adapter.simulate_degradation("critical")
        metrics = adapter.fetch_current_metrics()
        assert metrics["error_rate"] >= 0.3


class TestAutoRollbackGuard:
    """AutoRollbackGuard 테스트"""

    def setup_method(self):
        self.metrics_provider = Mock()
        self.metrics_provider.get_error_rate = Mock(return_value=0.02)
        self.metrics_provider.get_latency_p99 = Mock(return_value=200)
        self.metrics_provider.get_throughput = Mock(return_value=1000)
        self.config_applier = Mock()
        self.config_applier.apply = Mock(return_value=True)

    def test_initial_state(self):
        """초기 상태"""
        guard = AutoRollbackGuard(
            metrics_provider=self.metrics_provider,
            config_applier=self.config_applier,
            enabled=False,
        )
        assert guard.state == GuardState.INACTIVE

    def test_degradation_assessment(self):
        """저하 수준 평가"""
        guard = AutoRollbackGuard(
            metrics_provider=self.metrics_provider,
            config_applier=self.config_applier,
        )
        assert guard._assess_degradation(0.02, 200) == RollbackSeverity.NONE
        assert guard._assess_degradation(0.35, 12000) == RollbackSeverity.CRITICAL

    def test_start_stop(self):
        """시작/중지"""
        guard = AutoRollbackGuard(
            metrics_provider=self.metrics_provider,
            config_applier=self.config_applier,
            check_interval_seconds=1,
        )
        guard.start()
        assert guard.state == GuardState.MONITORING
        guard.stop()
        assert guard.state == GuardState.INACTIVE


class TestRuntimeFeedbackLoop:
    """RuntimeFeedbackLoop 테스트"""

    def setup_method(self):
        self.metrics_adapter = MockMetricsAdapter()
        self.config_provider = Mock()
        self.config_provider.get = Mock(
            side_effect=lambda key, default=None: {
                "timeout_ms": 5000.0,
            }.get(key, default)
        )
        self.config_applier = Mock()
        self.config_applier.get_current = Mock(return_value=5000.0)
        self.config_applier.apply = Mock(return_value=True)
        self.config_applier.rollback = Mock(return_value=True)
        self.decision_engine = DecisionEngine(self.config_provider)
        self.safety_bounds = SafetyBounds()
        self.audit_adapter = Mock()

    def test_initial_state(self):
        """초기 상태"""
        loop = RuntimeFeedbackLoop(
            metrics_adapter=self.metrics_adapter,
            decision_engine=self.decision_engine,
            safety_bounds=self.safety_bounds,
            audit_adapter=self.audit_adapter,
            alert_manager=Mock(),
            config_applier=self.config_applier,
            enabled=False,
        )
        assert loop.state == FeedbackLoopState.STOPPED

    def test_start_stop(self):
        """시작/중지"""
        loop = RuntimeFeedbackLoop(
            metrics_adapter=self.metrics_adapter,
            decision_engine=self.decision_engine,
            safety_bounds=self.safety_bounds,
            audit_adapter=self.audit_adapter,
            alert_manager=Mock(),
            config_applier=self.config_applier,
            interval_seconds=1,
        )
        loop.start()
        assert loop.state == FeedbackLoopState.RUNNING
        loop.stop()
        assert loop.state == FeedbackLoopState.STOPPED

    def test_pause_resume(self):
        """일시정지/재개"""
        loop = RuntimeFeedbackLoop(
            metrics_adapter=self.metrics_adapter,
            decision_engine=self.decision_engine,
            safety_bounds=self.safety_bounds,
            audit_adapter=self.audit_adapter,
            alert_manager=Mock(),
            config_applier=self.config_applier,
        )
        loop.start()
        loop.pause("test")
        assert loop.state == FeedbackLoopState.PAUSED
        loop.resume()
        assert loop.state == FeedbackLoopState.RUNNING
        loop.stop()

    def test_get_status(self):
        """상태 조회"""
        loop = RuntimeFeedbackLoop(
            metrics_adapter=self.metrics_adapter,
            decision_engine=self.decision_engine,
            safety_bounds=self.safety_bounds,
            audit_adapter=self.audit_adapter,
            alert_manager=Mock(),
            config_applier=self.config_applier,
        )
        status = loop.get_status()
        assert "state" in status
        assert "enabled" in status
