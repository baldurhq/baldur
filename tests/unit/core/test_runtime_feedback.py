"""
Tests for Runtime Feedback Loop - 실시간 메트릭 기반 자동 튜닝
"""

import threading
from datetime import UTC, datetime, timedelta

import pytest

from baldur.core.decision_engine import (
    DecisionEngine,
)
from baldur.core.runtime_feedback import (
    AdjustmentResult,
    FeedbackLoopState,
    RuntimeFeedbackLoop,
)
from baldur.core.safety_bounds import SafetyBounds

# =============================================================================
# Fixtures
# =============================================================================


class MockMetricsAdapter:
    """Mock metrics adapter for testing."""

    def __init__(self, metrics=None):
        self._metrics = metrics or {
            "error_rate": 0.01,
            "p99_latency_ms": 100,
            "retry_exhausted_rate": 0.01,
            "sample_count": 100,
        }

    def fetch_current_metrics(self):
        return self._metrics.copy()

    def set_metrics(self, metrics):
        self._metrics = metrics


class MockConfigProvider:
    """Mock config provider for DecisionEngine."""

    def __init__(self, config=None):
        self._config = config or {
            "timeout_ms": 3000,
            "retry_count": 3,
            "circuit_breaker_threshold": 0.5,
        }

    def get(self, key, default=None):
        return self._config.get(key, default)


class MockConfigApplier:
    """Mock config applier for testing."""

    def __init__(self):
        self._config = {
            "timeout_ms": 3000,
            "retry_count": 3,
            "circuit_breaker_threshold": 0.5,
        }
        self._apply_calls = []
        self._rollback_calls = []

    def get_current(self, parameter):
        return self._config.get(parameter)

    def apply(self, parameter, value):
        self._apply_calls.append((parameter, value))
        self._config[parameter] = value
        return True

    def rollback(self, parameter, value):
        self._rollback_calls.append((parameter, value))
        self._config[parameter] = value
        return True


class MockAuditAdapter:
    """Mock audit adapter for testing."""

    def __init__(self):
        self._logs = []

    def log(self, entry):
        self._logs.append(entry)


class MockAlertManager:
    """Mock alert manager for testing."""

    def __init__(self):
        self._alerts = []

    def send_auto_tuning_alert(self, **kwargs):
        self._alerts.append(kwargs)

    def _send_notification(self, **kwargs):
        self._alerts.append(kwargs)


@pytest.fixture
def metrics_adapter():
    return MockMetricsAdapter()


@pytest.fixture
def config_provider():
    return MockConfigProvider()


@pytest.fixture
def config_applier():
    return MockConfigApplier()


@pytest.fixture
def decision_engine(config_provider):
    return DecisionEngine(config_provider=config_provider)


@pytest.fixture
def safety_bounds():
    return SafetyBounds()


@pytest.fixture
def audit_adapter():
    return MockAuditAdapter()


@pytest.fixture
def alert_manager():
    return MockAlertManager()


@pytest.fixture
def feedback_loop(
    metrics_adapter,
    decision_engine,
    safety_bounds,
    audit_adapter,
    alert_manager,
    config_applier,
):
    """Create a RuntimeFeedbackLoop instance."""
    return RuntimeFeedbackLoop(
        metrics_adapter=metrics_adapter,
        decision_engine=decision_engine,
        safety_bounds=safety_bounds,
        audit_adapter=audit_adapter,
        alert_manager=alert_manager,
        config_applier=config_applier,
        enabled=True,
        interval_seconds=1,  # Fast for testing
        auto_rollback_enabled=True,
    )


@pytest.fixture
def disabled_feedback_loop(
    metrics_adapter,
    decision_engine,
    safety_bounds,
    audit_adapter,
    alert_manager,
    config_applier,
):
    """Create a disabled RuntimeFeedbackLoop instance."""
    return RuntimeFeedbackLoop(
        metrics_adapter=metrics_adapter,
        decision_engine=decision_engine,
        safety_bounds=safety_bounds,
        audit_adapter=audit_adapter,
        alert_manager=alert_manager,
        config_applier=config_applier,
        enabled=False,
    )


# =============================================================================
# Initialization Tests
# =============================================================================


class TestRuntimeFeedbackLoopInit:
    """Test RuntimeFeedbackLoop initialization."""

    def test_init_defaults(self, feedback_loop):
        """기본 초기화 상태."""
        assert feedback_loop.state == FeedbackLoopState.STOPPED
        assert feedback_loop.enabled is True
        assert feedback_loop.auto_rollback_enabled is True

    def test_init_disabled(self, disabled_feedback_loop):
        """비활성화 상태로 초기화."""
        assert disabled_feedback_loop.enabled is False


# =============================================================================
# Start/Stop Tests
# =============================================================================


class TestStartStop:
    """Test start/stop operations."""

    def test_start(self, feedback_loop):
        """피드백 루프 시작."""
        result = feedback_loop.start()

        assert result is True
        assert feedback_loop.state == FeedbackLoopState.RUNNING

        feedback_loop.stop()

    def test_start_when_already_running(self, feedback_loop):
        """이미 실행 중일 때 시작 시도."""
        feedback_loop.start()

        result = feedback_loop.start()

        assert result is False

        feedback_loop.stop()

    def test_stop(self, feedback_loop):
        """피드백 루프 중지."""
        feedback_loop.start()

        result = feedback_loop.stop()

        assert result is True
        assert feedback_loop.state == FeedbackLoopState.STOPPED

    def test_stop_when_not_running(self, feedback_loop):
        """실행 중이 아닐 때 중지."""
        result = feedback_loop.stop()

        assert result is True


# =============================================================================
# Pause/Resume Tests
# =============================================================================


class TestPauseResume:
    """Test pause/resume operations."""

    def test_pause(self, feedback_loop):
        """일시 정지."""
        feedback_loop.start()

        result = feedback_loop.pause("test_reason")

        assert result is True
        assert feedback_loop.state == FeedbackLoopState.PAUSED

        feedback_loop.stop()

    def test_resume(self, feedback_loop):
        """재개."""
        feedback_loop.start()
        feedback_loop.pause("test")

        result = feedback_loop.resume()

        assert result is True
        assert feedback_loop.state == FeedbackLoopState.RUNNING

        feedback_loop.stop()

    def test_resume_when_not_running(self, feedback_loop):
        """실행 중이 아닐 때 재개 시도."""
        result = feedback_loop.resume()

        assert result is False


# =============================================================================
# FeedbackLoopState Tests
# =============================================================================


class TestFeedbackLoopState:
    """Test FeedbackLoopState enum."""

    def test_states_exist(self):
        """모든 상태 확인."""
        assert FeedbackLoopState.STOPPED == "stopped"
        assert FeedbackLoopState.RUNNING == "running"
        assert FeedbackLoopState.PAUSED == "paused"
        assert FeedbackLoopState.ERROR == "error"


# =============================================================================
# AdjustmentResult Tests
# =============================================================================


class TestAdjustmentResult:
    """Test AdjustmentResult dataclass."""

    def test_result_creation(self):
        """AdjustmentResult 생성."""
        result = AdjustmentResult(
            success=True,
            parameter="timeout_ms",
            old_value=3000,
            new_value=3600,
            reason="P99 레이턴시 증가",
        )

        assert result.success is True
        assert result.parameter == "timeout_ms"
        assert result.old_value == 3000
        assert result.new_value == 3600

    def test_result_with_error(self):
        """실패 결과."""
        result = AdjustmentResult(
            success=False,
            parameter="timeout_ms",
            old_value=3000,
            new_value=3600,
            reason="test",
            error="Apply failed",
        )

        assert result.success is False
        assert result.error == "Apply failed"

    def test_result_timestamp(self):
        """타임스탬프 자동 설정."""
        result = AdjustmentResult(
            success=True,
            parameter="test",
            old_value=1,
            new_value=2,
            reason="test",
        )

        assert result.timestamp is not None


# =============================================================================
# Observe and Adjust Tests
# =============================================================================


class TestObserveAndAdjust:
    """Test observe_and_adjust method."""

    def test_no_adjustment_needed(self, feedback_loop, metrics_adapter):
        """조정이 필요 없는 경우."""
        metrics_adapter.set_metrics(
            {
                "error_rate": 0.01,
                "p99_latency_ms": 100,
                "sample_count": 100,
            }
        )

        result = feedback_loop.observe_and_adjust()

        assert result["adjusted"] is False
        assert result["reason"] == "no_adjustment_needed"

    def test_adjustment_made(self, feedback_loop, metrics_adapter, config_applier):
        """조정이 수행되는 경우."""
        metrics_adapter.set_metrics(
            {
                "p99_latency_ms": 2500,  # 타임아웃 상향 필요
                "sample_count": 100,
            }
        )

        result = feedback_loop.observe_and_adjust()

        assert result["adjusted"] is True
        assert len(result["adjustments"]) >= 1
        assert len(config_applier._apply_calls) >= 1

    def test_metrics_fetch_failure(self, feedback_loop, metrics_adapter):
        """메트릭 수집 실패."""

        def raise_error():
            raise Exception("Metrics fetch failed")

        metrics_adapter.fetch_current_metrics = raise_error

        result = feedback_loop.observe_and_adjust()

        assert result["adjusted"] is False
        assert result["reason"] == "metrics_fetch_failed"

    def test_rollback_cooldown(self, feedback_loop, metrics_adapter):
        """롤백 쿨다운 중."""
        feedback_loop._last_rollback_time = datetime.now(UTC)

        metrics_adapter.set_metrics(
            {
                "p99_latency_ms": 2500,
                "sample_count": 100,
            }
        )

        result = feedback_loop.observe_and_adjust()

        assert result["adjusted"] is False
        assert result["reason"] == "in_rollback_cooldown"


# =============================================================================
# Safety Bounds Integration Tests
# =============================================================================


class TestSafetyBoundsIntegration:
    """Test integration with SafetyBounds."""

    def test_adjustment_rejected_by_safety_bounds(
        self,
        metrics_adapter,
        config_provider,
        config_applier,
        audit_adapter,
        alert_manager,
    ):
        """안전 한계에 의해 조정 거부."""
        # 매우 제한적인 safety bounds 설정
        strict_bounds = SafetyBounds(
            custom_bounds={
                "timeout_ms": {
                    "min_value": 3000,
                    "max_value": 3000,  # 변경 불가
                    "max_change_per_cycle": 0.01,
                }
            }
        )

        decision_engine = DecisionEngine(config_provider=config_provider)

        loop = RuntimeFeedbackLoop(
            metrics_adapter=metrics_adapter,
            decision_engine=decision_engine,
            safety_bounds=strict_bounds,
            audit_adapter=audit_adapter,
            alert_manager=alert_manager,
            config_applier=config_applier,
        )

        metrics_adapter.set_metrics(
            {
                "p99_latency_ms": 2500,
                "sample_count": 100,
            }
        )

        result = loop.observe_and_adjust()

        # 조정이 거부되거나 제한됨
        if result["adjusted"]:
            for adj in result["adjustments"]:
                if adj["parameter"] == "timeout_ms":
                    # 변경이 제한됨
                    assert adj["new_value"] <= 3000


# =============================================================================
# Degradation Detection Tests
# =============================================================================


class TestDegradationDetection:
    """Test degradation detection."""

    def test_detect_error_rate_increase(self, feedback_loop):
        """에러율 증가 감지."""
        pre_metrics = {"error_rate": 0.01, "p99_latency_ms": 100}
        post_metrics = {"error_rate": 0.05, "p99_latency_ms": 100}  # 400% 증가

        degraded = feedback_loop._detect_degradation(pre_metrics, post_metrics)

        assert degraded is True

    def test_detect_latency_increase(self, feedback_loop):
        """레이턴시 증가 감지."""
        pre_metrics = {"error_rate": 0.01, "p99_latency_ms": 100}
        post_metrics = {"error_rate": 0.01, "p99_latency_ms": 200}  # 100% 증가

        degraded = feedback_loop._detect_degradation(pre_metrics, post_metrics)

        assert degraded is True

    def test_no_degradation(self, feedback_loop):
        """저하 없음."""
        pre_metrics = {"error_rate": 0.01, "p99_latency_ms": 100}
        post_metrics = {"error_rate": 0.01, "p99_latency_ms": 105}  # 5% 증가

        degraded = feedback_loop._detect_degradation(pre_metrics, post_metrics)

        assert degraded is False

    def test_zero_to_error(self, feedback_loop):
        """0에서 에러 발생."""
        pre_metrics = {"error_rate": 0, "p99_latency_ms": 100}
        post_metrics = {"error_rate": 0.06, "p99_latency_ms": 100}  # 0→6%

        degraded = feedback_loop._detect_degradation(pre_metrics, post_metrics)

        assert degraded is True


# =============================================================================
# Rollback Tests
# =============================================================================


class TestRollback:
    """Test rollback operations."""

    def test_manual_rollback(self, feedback_loop, config_applier):
        """수동 롤백."""
        # 스냅샷 저장
        feedback_loop._snapshot_before_adjustment["timeout_ms"] = 3000
        config_applier._config["timeout_ms"] = 4000

        result = feedback_loop.manual_rollback("timeout_ms")

        assert result is True
        assert ("timeout_ms", 3000) in config_applier._rollback_calls

    def test_manual_rollback_no_snapshot(self, feedback_loop):
        """스냅샷 없이 롤백 시도."""
        result = feedback_loop.manual_rollback("unknown_param")

        assert result is False


# =============================================================================
# Get Status Tests
# =============================================================================


class TestGetStatus:
    """Test get_status method."""

    def test_get_status_initial(self, feedback_loop):
        """초기 상태 조회."""
        status = feedback_loop.get_status()

        assert status["state"] == "stopped"
        assert status["enabled"] is True
        assert status["consecutive_failures"] == 0

    def test_get_status_running(self, feedback_loop):
        """실행 중 상태 조회."""
        feedback_loop.start()

        status = feedback_loop.get_status()

        assert status["state"] == "running"

        feedback_loop.stop()

    def test_get_status_with_adjustments(self, feedback_loop):
        """조정 이력 포함 상태 조회."""
        result = AdjustmentResult(
            success=True,
            parameter="test",
            old_value=1,
            new_value=2,
            reason="test",
        )
        feedback_loop._adjustment_history.append(result)

        status = feedback_loop.get_status()

        assert status["adjustment_count"] == 1
        assert len(status["last_adjustments"]) == 1


# =============================================================================
# Cooldown Tests
# =============================================================================


class TestCooldown:
    """Test cooldown logic."""

    def test_in_rollback_cooldown(self, feedback_loop):
        """롤백 쿨다운 중."""
        feedback_loop._last_rollback_time = datetime.now(UTC)

        assert feedback_loop._is_in_rollback_cooldown() is True

    def test_not_in_rollback_cooldown(self, feedback_loop):
        """롤백 쿨다운 지남."""
        feedback_loop._last_rollback_time = datetime.now(UTC) - timedelta(minutes=5)

        assert feedback_loop._is_in_rollback_cooldown() is False

    def test_no_rollback_history(self, feedback_loop):
        """롤백 이력 없음."""
        assert feedback_loop._is_in_rollback_cooldown() is False


# =============================================================================
# Consecutive Failures Tests
# =============================================================================


class TestConsecutiveFailures:
    """Test consecutive failure handling."""

    def test_max_consecutive_failures_pause(self, feedback_loop):
        """연속 실패 시 일시 정지."""
        # 인스턴스 property로 접근
        feedback_loop._consecutive_failures = feedback_loop.MAX_CONSECUTIVE_FAILURES

        # 에러 핸들링
        feedback_loop._handle_loop_error(Exception("Test error"))

        assert feedback_loop.state == FeedbackLoopState.ERROR

    def test_successful_adjustment_resets_failures(
        self, feedback_loop, metrics_adapter, config_applier
    ):
        """성공적인 조정 시 실패 카운터 리셋."""
        feedback_loop._consecutive_failures = 2

        metrics_adapter.set_metrics(
            {
                "p99_latency_ms": 2500,
                "sample_count": 100,
            }
        )

        result = feedback_loop.observe_and_adjust()

        if result["adjusted"]:
            assert feedback_loop._consecutive_failures == 0


# =============================================================================
# Baseline Metrics Tests
# =============================================================================


class TestBaselineMetrics:
    """Test baseline metrics tracking."""

    def test_baseline_saved_on_first_run(self, feedback_loop, metrics_adapter):
        """첫 실행 시 베이스라인 저장."""
        assert feedback_loop._baseline_metrics is None

        feedback_loop.observe_and_adjust()

        assert feedback_loop._baseline_metrics is not None

    def test_baseline_not_overwritten(self, feedback_loop, metrics_adapter):
        """베이스라인이 덮어씌워지지 않음."""
        feedback_loop.observe_and_adjust()

        first_baseline = feedback_loop._baseline_metrics.copy()

        metrics_adapter.set_metrics(
            {
                "error_rate": 0.1,
                "p99_latency_ms": 500,
                "sample_count": 100,
            }
        )

        feedback_loop.observe_and_adjust()

        assert feedback_loop._baseline_metrics == first_baseline


# =============================================================================
# Edge Cases
# =============================================================================


class TestEdgeCases:
    """Test edge cases."""

    def test_config_applier_exception(
        self,
        metrics_adapter,
        config_provider,
        safety_bounds,
        audit_adapter,
        alert_manager,
    ):
        """config_applier 예외 처리."""
        failing_applier = MockConfigApplier()

        def raise_error(param, value):
            raise Exception("Apply failed")

        failing_applier.apply = raise_error

        decision_engine = DecisionEngine(config_provider=config_provider)

        loop = RuntimeFeedbackLoop(
            metrics_adapter=metrics_adapter,
            decision_engine=decision_engine,
            safety_bounds=safety_bounds,
            audit_adapter=audit_adapter,
            alert_manager=alert_manager,
            config_applier=failing_applier,
        )

        metrics_adapter.set_metrics(
            {
                "p99_latency_ms": 2500,
                "sample_count": 100,
            }
        )

        # 예외가 발생해도 처리됨
        result = loop.observe_and_adjust()
        assert "adjusted" in result

    def test_empty_decisions(self, feedback_loop, metrics_adapter):
        """조정 결정이 없는 경우."""
        metrics_adapter.set_metrics(
            {
                "error_rate": 0.001,
                "p99_latency_ms": 50,
                "sample_count": 100,
            }
        )

        result = feedback_loop.observe_and_adjust()

        assert result["adjusted"] is False


# =============================================================================
# Thread Safety Tests
# =============================================================================


class TestThreadSafety:
    """Test thread safety."""

    def test_concurrent_status_reads(self, feedback_loop):
        """동시 상태 읽기."""
        feedback_loop.start()

        results = []

        def read_status():
            for _ in range(50):
                feedback_loop.get_status()
            results.append(True)

        threads = [threading.Thread(target=read_status) for _ in range(5)]

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        feedback_loop.stop()

        assert len(results) == 5


# =============================================================================
# Constants Tests
# =============================================================================


class TestConstants:
    """Test constant values via settings."""

    def test_max_consecutive_failures(self):
        """MAX_CONSECUTIVE_FAILURES 상수 (인스턴스 속성으로 확인)."""
        from baldur.settings.runtime_feedback import get_runtime_feedback_settings

        settings = get_runtime_feedback_settings()
        assert settings.max_consecutive_failures == 3

    def test_post_rollback_cooldown(self):
        """POST_ROLLBACK_COOLDOWN 상수 (인스턴스 속성으로 확인)."""
        from baldur.settings.runtime_feedback import get_runtime_feedback_settings

        settings = get_runtime_feedback_settings()
        assert settings.rollback_cooldown == 120

    def test_post_adjustment_wait(self):
        """POST_ADJUSTMENT_WAIT 상수 (인스턴스 속성으로 확인)."""
        from baldur.settings.runtime_feedback import get_runtime_feedback_settings

        settings = get_runtime_feedback_settings()
        assert settings.adjustment_wait == 30
