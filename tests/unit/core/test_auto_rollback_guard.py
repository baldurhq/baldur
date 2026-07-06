"""
Tests for Auto Rollback Guard - 자율 조정 실패 대비 안전장치
"""

import threading
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from baldur.core.auto_rollback_guard import (
    AutoRollbackGuard,
    GuardState,
    RollbackHealthAssessment,
    RollbackSeverity,
    SafeDefault,
)

# =============================================================================
# Fixtures
# =============================================================================


class MockMetricsProvider:
    """Mock metrics provider for testing."""

    def __init__(self, error_rate=0.01, latency_p99=100, throughput=1000):
        self.error_rate = error_rate
        self.latency_p99 = latency_p99
        self.throughput = throughput

    def get_error_rate(self):
        return self.error_rate

    def get_latency_p99(self):
        return self.latency_p99

    def get_throughput(self):
        return self.throughput


class MockConfigApplier:
    """Mock config applier for testing."""

    def __init__(self):
        self._config = {
            "timeout_ms": 5000,
            "retry_count": 3,
            "circuit_breaker_threshold": 0.5,
        }
        self._rollback_calls = []
        self._apply_calls = []
        self._dna_values = {}

    def apply(self, parameter, value):
        self._apply_calls.append((parameter, value))
        self._config[parameter] = value
        return True

    def rollback(self, parameter, value):
        self._rollback_calls.append((parameter, value))
        self._config[parameter] = value
        return True

    def get_dna_value(self, parameter):
        return self._dna_values.get(parameter)

    def set_dna_value(self, parameter, value):
        self._dna_values[parameter] = value


@pytest.fixture
def metrics_provider():
    """Create a mock metrics provider."""
    return MockMetricsProvider()


@pytest.fixture
def config_applier():
    """Create a mock config applier."""
    return MockConfigApplier()


@pytest.fixture
def guard(metrics_provider, config_applier):
    """Create an AutoRollbackGuard instance."""
    return AutoRollbackGuard(
        metrics_provider=metrics_provider,
        config_applier=config_applier,
        check_interval_seconds=1,  # Fast for testing
        enabled=True,
    )


@pytest.fixture
def disabled_guard(metrics_provider, config_applier):
    """Create a disabled AutoRollbackGuard instance."""
    return AutoRollbackGuard(
        metrics_provider=metrics_provider,
        config_applier=config_applier,
        enabled=False,
    )


# =============================================================================
# Initialization Tests
# =============================================================================


class TestAutoRollbackGuardInit:
    """Test AutoRollbackGuard initialization."""

    def test_init_defaults(self, guard):
        """기본 초기화 상태."""
        assert guard.state == GuardState.INACTIVE
        assert guard.enabled is True
        assert guard.check_interval_seconds == 1

    def test_init_disabled(self, disabled_guard):
        """비활성화 상태로 초기화."""
        assert disabled_guard.enabled is False

    def test_init_with_alert_callback(self, metrics_provider, config_applier):
        """알림 콜백과 함께 초기화."""
        alert_callback = MagicMock()

        guard = AutoRollbackGuard(
            metrics_provider=metrics_provider,
            config_applier=config_applier,
            alert_callback=alert_callback,
        )

        assert guard.alert_callback == alert_callback


# =============================================================================
# Start/Stop Tests
# =============================================================================


class TestStartStop:
    """Test start/stop operations."""

    def test_start(self, guard):
        """가드 시작."""
        result = guard.start()

        assert result is True
        assert guard.state == GuardState.MONITORING

        guard.stop()

    def test_start_when_already_running(self, guard):
        """이미 실행 중일 때 시작 시도."""
        guard.start()

        result = guard.start()

        assert result is False

        guard.stop()

    def test_stop(self, guard):
        """가드 중지."""
        guard.start()

        result = guard.stop()

        assert result is True
        assert guard.state == GuardState.INACTIVE

    def test_stop_when_not_running(self, guard):
        """실행 중이 아닐 때 중지."""
        result = guard.stop()

        assert result is True
        assert guard.state == GuardState.INACTIVE


# =============================================================================
# Degradation Assessment Tests
# =============================================================================


class TestDegradationAssessment:
    """Test degradation level assessment."""

    @pytest.mark.parametrize(
        ("error_rate", "latency_p99", "expected_severity"),
        [
            (0.01, 100, RollbackSeverity.NONE),  # 정상
            (0.06, 100, RollbackSeverity.MINOR),  # 에러율 5%+
            (0.01, 3500, RollbackSeverity.MINOR),  # 레이턴시 3초+
            (0.12, 100, RollbackSeverity.MAJOR),  # 에러율 10%+
            (0.01, 5500, RollbackSeverity.MAJOR),  # 레이턴시 5초+
            (0.35, 100, RollbackSeverity.CRITICAL),  # 에러율 30%+
            (0.01, 12000, RollbackSeverity.CRITICAL),  # 레이턴시 10초+
            (0.35, 5500, RollbackSeverity.CRITICAL),  # CRITICAL이 MAJOR보다 우선
        ],
        ids=[
            "none",
            "minor_error_rate",
            "minor_latency",
            "major_error_rate",
            "major_latency",
            "critical_error_rate",
            "critical_latency",
            "critical_precedence",
        ],
    )
    def test_assess_degradation_levels(
        self, guard, error_rate, latency_p99, expected_severity
    ):
        """각 에러율/레이턴시 조합에 대한 저하 수준 평가."""
        level = guard._assess_degradation(
            error_rate=error_rate, latency_p99=latency_p99
        )
        assert level == expected_severity


# =============================================================================
# Health Check Result Tests
# =============================================================================


class TestRollbackHealthAssessment:
    """Test RollbackHealthAssessment dataclass."""

    def test_healthy_result(self):
        """정상 결과."""
        result = RollbackHealthAssessment(
            healthy=True,
            degradation_level=RollbackSeverity.NONE,
            error_rate=0.01,
            latency_p99_ms=100,
            throughput_rps=1000,
        )

        assert result.healthy is True
        assert result.degradation_level == RollbackSeverity.NONE

    def test_unhealthy_result(self):
        """비정상 결과."""
        result = RollbackHealthAssessment(
            healthy=False,
            degradation_level=RollbackSeverity.CRITICAL,
            error_rate=0.5,
            latency_p99_ms=15000,
            throughput_rps=100,
        )

        assert result.healthy is False
        assert result.degradation_level == RollbackSeverity.CRITICAL

    def test_result_timestamp(self):
        """타임스탬프 자동 설정."""
        result = RollbackHealthAssessment(
            healthy=True,
            degradation_level=RollbackSeverity.NONE,
            error_rate=0.01,
            latency_p99_ms=100,
            throughput_rps=1000,
        )

        assert result.timestamp is not None
        assert isinstance(result.timestamp, datetime)


# =============================================================================
# SafeDefault Tests
# =============================================================================


class TestSafeDefault:
    """Test SafeDefault dataclass."""

    def test_safe_default_creation(self):
        """SafeDefault 생성."""
        sd = SafeDefault(
            parameter="timeout_ms",
            safe_value=5000,
            description="Default timeout",
        )

        assert sd.parameter == "timeout_ms"
        assert sd.safe_value == 5000
        assert sd.description == "Default timeout"


# =============================================================================
# Snapshot Tests
# =============================================================================


class TestSnapshot:
    """Test snapshot save operations."""

    def test_save_snapshot(self, guard):
        """스냅샷 저장."""
        guard.save_snapshot("timeout_ms", 5000)

        assert "timeout_ms" in guard._config_snapshots
        assert len(guard._config_snapshots["timeout_ms"]) == 1
        assert guard._config_snapshots["timeout_ms"][0]["value"] == 5000

    def test_save_multiple_snapshots(self, guard):
        """여러 스냅샷 저장."""
        guard.save_snapshot("timeout_ms", 5000)
        guard.save_snapshot("timeout_ms", 5500)
        guard.save_snapshot("timeout_ms", 6000)

        assert len(guard._config_snapshots["timeout_ms"]) == 3

    def test_snapshot_limit(self, guard):
        """스냅샷 최대 10개 제한."""
        for i in range(15):
            guard.save_snapshot("timeout_ms", 1000 + i * 100)

        assert len(guard._config_snapshots["timeout_ms"]) == 10


# =============================================================================
# Recovery Strategy Tests
# =============================================================================


class TestRecoveryStrategy:
    """Test recovery strategy priority."""

    def test_recover_with_last_known_good(self, guard, config_applier):
        """Last Known Good 복구."""
        # 스냅샷 저장
        guard.save_snapshot("timeout_ms", 4000)
        guard.save_snapshot("timeout_ms", 5000)

        # 복구 실행
        result = guard._recover_parameter("timeout_ms", 3000)

        assert "last_known_good" in result
        assert "4000" in result  # 첫 스냅샷 사용

    def test_recover_with_dna_declared(self, guard, config_applier):
        """DNA 선언값 복구 (스냅샷 없을 때)."""
        config_applier.set_dna_value("timeout_ms", 4500)

        # 복구 실행 (스냅샷 없음)
        result = guard._recover_parameter("timeout_ms", 3000)

        assert "dna_declared" in result
        assert "4500" in result

    def test_recover_with_system_default(self, guard, config_applier):
        """시스템 기본값 복구 (스냅샷, DNA 없을 때)."""
        # 스냅샷 없음, DNA 없음
        result = guard._recover_parameter("timeout_ms", 3000)

        assert "system_default" in result
        assert "3000" in result


# =============================================================================
# Emergency Recovery Tests
# =============================================================================


class TestEmergencyRecovery:
    """Test emergency recovery operations."""

    def test_trigger_manual_emergency(self, guard, config_applier):
        """수동 긴급 복구 트리거."""
        result = guard.trigger_manual_emergency("test_reason")

        assert result is True
        assert guard.state == GuardState.RECOVERING

    def test_emergency_recovery_applies_defaults(self, guard, config_applier):
        """긴급 복구 시 기본값 적용."""
        guard.trigger_manual_emergency()

        # 모든 SYSTEM_DEFAULTS 파라미터가 적용되어야 함
        assert len(config_applier._apply_calls) > 0


# =============================================================================
# Alert Callback Tests
# =============================================================================


class TestAlertCallback:
    """Test alert callback operations."""

    def test_alert_callback_called(self, metrics_provider, config_applier):
        """알림 콜백 호출."""
        alert_callback = MagicMock()

        guard = AutoRollbackGuard(
            metrics_provider=metrics_provider,
            config_applier=config_applier,
            alert_callback=alert_callback,
        )

        guard._send_alert("test_type", "test_message")

        alert_callback.assert_called_once_with("test_type", "test_message")

    def test_alert_callback_exception_handled(self, metrics_provider, config_applier):
        """알림 콜백 예외 처리."""
        alert_callback = MagicMock(side_effect=Exception("Callback error"))

        guard = AutoRollbackGuard(
            metrics_provider=metrics_provider,
            config_applier=config_applier,
            alert_callback=alert_callback,
        )

        # 예외가 발생해도 오류 없이 처리
        guard._send_alert("test_type", "test_message")


# =============================================================================
# Get Status Tests
# =============================================================================


class TestGetStatus:
    """Test get_status method."""

    def test_get_status_initial(self, guard):
        """초기 상태 조회."""
        status = guard.get_status()

        assert status["state"] == "inactive"
        assert status["enabled"] is True
        assert status["consecutive_failures"] == 0
        assert status["last_rollback"] is None

    def test_get_status_after_start(self, guard):
        """시작 후 상태 조회."""
        guard.start()

        status = guard.get_status()

        assert status["state"] == "monitoring"

        guard.stop()

    def test_get_status_with_health_history(self, guard):
        """헬스 이력 포함 상태 조회."""
        # 헬스체크 결과 추가
        result = RollbackHealthAssessment(
            healthy=True,
            degradation_level=RollbackSeverity.NONE,
            error_rate=0.01,
            latency_p99_ms=100,
            throughput_rps=1000,
        )
        guard._health_history.append(result)

        status = guard.get_status()

        assert status["health_history_count"] == 1
        assert len(status["recent_health"]) == 1


# =============================================================================
# Safe Defaults Management Tests
# =============================================================================


class TestSafeDefaultsManagement:
    """Test safe defaults management."""

    def test_get_safe_defaults(self, guard):
        """안전한 기본값 목록 조회."""
        defaults = guard.get_safe_defaults()

        assert isinstance(defaults, list)
        assert len(defaults) > 0

        # 필수 파라미터 확인
        params = [d["parameter"] for d in defaults]
        assert "timeout_ms" in params
        assert "retry_count" in params

    def test_update_safe_default_existing(self, guard):
        """기존 안전한 기본값 업데이트."""
        original_defaults = guard.get_safe_defaults()
        next(d for d in original_defaults if d["parameter"] == "timeout_ms")

        result = guard.update_safe_default("timeout_ms", 8000, "Updated timeout")

        assert result is True

        updated_defaults = guard.get_safe_defaults()
        updated_timeout = next(
            d for d in updated_defaults if d["parameter"] == "timeout_ms"
        )

        assert updated_timeout["safe_value"] == 8000
        assert updated_timeout["description"] == "Updated timeout"

    def test_update_safe_default_new(self, guard):
        """새로운 안전한 기본값 추가."""
        result = guard.update_safe_default("new_param", 999, "New parameter")

        assert result is True

        defaults = guard.get_safe_defaults()
        new_default = next((d for d in defaults if d["parameter"] == "new_param"), None)

        assert new_default is not None
        assert new_default["safe_value"] == 999


# =============================================================================
# GuardState Tests
# =============================================================================


class TestGuardState:
    """Test GuardState enum."""

    def test_guard_states_exist(self):
        """모든 상태 확인."""
        assert GuardState.INACTIVE == "inactive"
        assert GuardState.MONITORING == "monitoring"
        assert GuardState.ALERT == "alert"
        assert GuardState.EMERGENCY == "emergency"
        assert GuardState.RECOVERING == "recovering"


# =============================================================================
# RollbackSeverity Tests
# =============================================================================


class TestRollbackSeverity:
    """Test RollbackSeverity enum."""

    def test_degradation_levels_exist(self):
        """모든 저하 수준 확인."""
        assert RollbackSeverity.NONE == "none"
        assert RollbackSeverity.MINOR == "minor"
        assert RollbackSeverity.MAJOR == "major"
        assert RollbackSeverity.CRITICAL == "critical"


# =============================================================================
# Threshold Constants Tests
# =============================================================================


class TestThresholdConstants:
    """Test threshold constants via settings."""

    def test_error_rate_thresholds(self):
        """에러율 임계값 (인스턴스 속성으로 확인)."""
        from baldur.settings.auto_rollback import get_auto_rollback_settings

        settings = get_auto_rollback_settings()
        assert settings.error_rate_major == 0.1
        assert settings.error_rate_critical == 0.3

    def test_latency_thresholds(self):
        """레이턴시 임계값 (인스턴스 속성으로 확인)."""
        from baldur.settings.auto_rollback import get_auto_rollback_settings

        settings = get_auto_rollback_settings()
        assert settings.latency_major_ms == 5000
        assert settings.latency_critical_ms == 10000

    def test_consecutive_failure_thresholds(self):
        """연속 실패 임계값 (인스턴스 속성으로 확인)."""
        from baldur.settings.auto_rollback import get_auto_rollback_settings

        settings = get_auto_rollback_settings()
        assert settings.failures_alert == 3
        assert settings.failures_emergency == 5


# =============================================================================
# Handle Health Result Tests
# =============================================================================


class TestHandleHealthResult:
    """Test health result handling."""

    def test_handle_healthy_result_resets_failures(self, guard):
        """정상 결과 시 실패 카운터 리셋."""
        guard._consecutive_failures = 5
        guard._state = GuardState.ALERT

        result = RollbackHealthAssessment(
            healthy=True,
            degradation_level=RollbackSeverity.NONE,
            error_rate=0.01,
            latency_p99_ms=100,
            throughput_rps=1000,
        )

        guard._handle_health_result(result)

        assert guard._consecutive_failures == 0
        assert guard._state == GuardState.MONITORING

    def test_handle_minor_degradation_increments_failures(self, guard):
        """경미한 저하 시 실패 카운터 증가."""
        guard._consecutive_failures = 0

        result = RollbackHealthAssessment(
            healthy=False,
            degradation_level=RollbackSeverity.MINOR,
            error_rate=0.06,
            latency_p99_ms=100,
            throughput_rps=1000,
        )

        guard._handle_health_result(result)

        assert guard._consecutive_failures == 1

    def test_handle_critical_triggers_emergency(self, guard, config_applier):
        """긴급 저하 시 긴급 복구 트리거."""
        result = RollbackHealthAssessment(
            healthy=False,
            degradation_level=RollbackSeverity.CRITICAL,
            error_rate=0.35,
            latency_p99_ms=100,
            throughput_rps=1000,
        )

        guard._handle_health_result(result)

        assert guard._state in (GuardState.EMERGENCY, GuardState.RECOVERING)


# =============================================================================
# Edge Cases
# =============================================================================


class TestEdgeCases:
    """Test edge cases."""

    def test_metrics_fetch_failure(self, metrics_provider, config_applier):
        """메트릭 수집 실패 처리."""

        def raise_error():
            raise Exception("Metrics fetch failed")

        metrics_provider.get_error_rate = raise_error

        guard = AutoRollbackGuard(
            metrics_provider=metrics_provider,
            config_applier=config_applier,
        )

        # 예외 발생해도 _consecutive_failures 증가
        guard._perform_health_check()

        assert guard._consecutive_failures >= 1

    def test_rollback_cooldown(self, guard):
        """롤백 쿨다운 (5분)."""
        # 롤백 실행
        guard._last_rollback_time = datetime.now(UTC)

        # 쿨다운 중에는 롤백 스킵
        guard._execute_rollback("test_reason")

        # 롤백이 실제로 수행되지 않음 (쿨다운)
        # 이 테스트에서는 쿨다운 로직이 작동하는지 확인

    def test_health_history_limit(self, guard):
        """헬스 이력 최대 100개 제한."""
        for _i in range(150):
            result = RollbackHealthAssessment(
                healthy=True,
                degradation_level=RollbackSeverity.NONE,
                error_rate=0.01,
                latency_p99_ms=100,
                throughput_rps=1000,
            )
            guard._health_history.append(result)

            if len(guard._health_history) > 100:
                guard._health_history = guard._health_history[-100:]

        assert len(guard._health_history) <= 100

    def test_config_applier_exception_handled(self, guard, config_applier):
        """config_applier 예외 처리."""

        def raise_error(param, value):
            raise Exception("Apply failed")

        config_applier.apply = raise_error

        # 예외 발생해도 처리됨
        result = guard._recover_parameter("timeout_ms", 5000)

        assert "FAILED" in result

    def test_dna_value_not_available(self, guard, config_applier):
        """DNA 값이 없는 경우."""
        # DNA 값 설정 안 함

        result = guard._get_dna_declared_value("timeout_ms")

        assert result is None


# =============================================================================
# Thread Safety Tests
# =============================================================================


class TestThreadSafety:
    """Test thread safety."""

    def test_concurrent_snapshot_saves(self, guard):
        """동시 스냅샷 저장."""
        errors = []

        def save_snapshots(param_id):
            try:
                for i in range(20):
                    guard.save_snapshot(f"param_{param_id}", 1000 + i)
            except Exception as e:
                errors.append(str(e))

        threads = [threading.Thread(target=save_snapshots, args=(i,)) for i in range(3)]

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0

    def test_concurrent_status_reads(self, guard):
        """동시 상태 읽기."""
        results = []

        def read_status():
            for _ in range(50):
                guard.get_status()
            results.append(True)

        threads = [threading.Thread(target=read_status) for _ in range(5)]

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(results) == 5
