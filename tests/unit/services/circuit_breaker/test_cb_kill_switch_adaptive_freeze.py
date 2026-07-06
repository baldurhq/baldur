"""
Circuit Breaker Kill Switch and Adaptive Freeze Tests

테스트 대상:
1. Kill Switch Override (manual_control.py 수정)
2. Adaptive Threshold (adaptive_threshold.py)
3. Freeze Mode (freeze_mode.py)
4. Panic Threshold (panic_threshold.py)
"""

from unittest.mock import Mock, patch

import pytest

# =============================================================================
# Kill Switch Override Tests
# =============================================================================


class TestKillSwitchOverride:
    """Kill Switch Override 기능 테스트 (manual_control.py)."""

    def test_force_open_blocked_when_kill_switch_active_without_override(self):
        """Kill Switch 활성 시 override 없으면 force_open 차단."""
        from baldur.services.circuit_breaker.config import (
            CircuitBreakerConfig,
        )
        from baldur.services.circuit_breaker.manual_control import (
            ManualControlMixin,
        )

        # Mock repository
        mock_repo = Mock()

        # Create mixin instance with mocked config
        mixin = ManualControlMixin()
        mixin.config = CircuitBreakerConfig()
        mixin.repository = mock_repo

        with patch(
            "baldur.services.circuit_breaker.manual_control._is_system_enabled",
            return_value=False,
        ):
            result = mixin.force_open(
                service_name="payment-api",
                reason="test",
            )

        assert result.success is False
        assert "Kill Switch" in result.error
        assert "override_kill_switch=True" in result.error

    def test_force_open_allowed_with_override(self):
        """Kill Switch 활성 시 override=True면 force_open 허용."""
        from baldur.services.circuit_breaker.config import (
            CircuitBreakerConfig,
        )
        from baldur.services.circuit_breaker.manual_control import (
            ManualControlMixin,
        )

        # Mock repository
        mock_repo = Mock()
        mock_repo.atomic_force_open.return_value = (True, "closed", "open")

        # Create mixin instance
        mixin = ManualControlMixin()
        mixin.config = CircuitBreakerConfig()
        mixin.repository = mock_repo
        mixin._half_open_requests = {}
        mixin._emit_event = Mock()

        with patch(
            "baldur.services.circuit_breaker.manual_control._is_system_enabled",
            return_value=False,
        ):
            with patch("baldur.services.circuit_breaker.manual_control.logger"):
                result = mixin.force_open(
                    service_name="payment-api",
                    reason="emergency",
                    override_kill_switch=True,
                )

        assert result.success is True
        mock_repo.atomic_force_open.assert_called_once()

    def test_force_close_blocked_when_kill_switch_active_without_override(self):
        """Kill Switch 활성 시 override 없으면 force_close 차단."""
        from baldur.services.circuit_breaker.config import CircuitBreakerConfig
        from baldur.services.circuit_breaker.manual_control import (
            ManualControlMixin,
        )

        mock_repo = Mock()
        mixin = ManualControlMixin()
        mixin.config = CircuitBreakerConfig()
        mixin.repository = mock_repo

        with patch(
            "baldur.services.circuit_breaker.manual_control._is_system_enabled",
            return_value=False,
        ):
            result = mixin.force_close(
                service_name="payment-api",
                reason="test",
            )

        assert result.success is False
        assert "Kill Switch" in result.error

    def test_force_close_allowed_with_override(self):
        """Kill Switch 활성 시 override=True면 force_close 허용."""
        from baldur.services.circuit_breaker.config import CircuitBreakerConfig
        from baldur.services.circuit_breaker.manual_control import (
            ManualControlMixin,
        )

        mock_repo = Mock()
        mock_repo.atomic_force_close.return_value = (True, "open", "closed")

        mixin = ManualControlMixin()
        mixin.config = CircuitBreakerConfig()
        mixin.repository = mock_repo
        mixin._half_open_requests = {}
        mixin._emit_event = Mock()

        with patch(
            "baldur.services.circuit_breaker.manual_control._is_system_enabled",
            return_value=False,
        ):
            with patch("baldur.services.circuit_breaker.manual_control.logger"):
                result = mixin.force_close(
                    service_name="payment-api",
                    reason="recovery",
                    override_kill_switch=True,
                )

        assert result.success is True
        mock_repo.atomic_force_close.assert_called_once()


# =============================================================================
# Adaptive Threshold Tests
# =============================================================================


class TestAdaptiveThreshold:
    """Adaptive Threshold 기능 테스트 (adaptive_threshold.py)."""

    def test_get_adjusted_threshold_normal(self):
        """NORMAL 레벨에서 기본 임계값 반환."""
        from baldur.services.circuit_breaker.adaptive_threshold import (
            AdaptiveThresholdManager,
            AdaptiveThresholdPolicy,
        )

        manager = AdaptiveThresholdManager(
            policy=AdaptiveThresholdPolicy(
                base_failure_threshold=5,
                base_window_seconds=60,
            )
        )

        threshold = manager.get_adjusted_threshold(emergency_level="NORMAL")

        assert threshold.failure_threshold == 5.0
        assert threshold.window_seconds == 60.0
        assert threshold.emergency_level == "NORMAL"
        assert threshold.is_lockdown is False

    def test_get_adjusted_threshold_elevated(self):
        """ELEVATED 레벨에서 1.5배 보수적 임계값."""
        from baldur.services.circuit_breaker.adaptive_threshold import (
            AdaptiveThresholdManager,
        )

        manager = AdaptiveThresholdManager()
        threshold = manager.get_adjusted_threshold(emergency_level="ELEVATED")

        assert threshold.failure_threshold == 7.5  # 5 * 1.5
        assert threshold.window_seconds == 90.0  # 60 * 1.5
        assert threshold.is_lockdown is False

    def test_get_adjusted_threshold_high(self):
        """HIGH 레벨에서 2배 보수적 임계값."""
        from baldur.services.circuit_breaker.adaptive_threshold import (
            AdaptiveThresholdManager,
        )

        manager = AdaptiveThresholdManager()
        threshold = manager.get_adjusted_threshold(emergency_level="HIGH")

        assert threshold.failure_threshold == 10.0  # 5 * 2.0
        assert threshold.window_seconds == 120.0  # 60 * 2.0
        assert threshold.is_lockdown is False

    def test_get_adjusted_threshold_critical(self):
        """CRITICAL 레벨에서 3배 보수적 임계값."""
        from baldur.services.circuit_breaker.adaptive_threshold import (
            AdaptiveThresholdManager,
        )

        manager = AdaptiveThresholdManager()
        threshold = manager.get_adjusted_threshold(emergency_level="CRITICAL")

        assert threshold.failure_threshold == 15.0  # 5 * 3.0
        assert threshold.window_seconds == 180.0  # 60 * 3.0
        assert threshold.is_lockdown is False

    def test_get_adjusted_threshold_lockdown(self):
        """LOCKDOWN 레벨에서 무한대 임계값 (자동 OPEN 금지)."""
        from baldur.services.circuit_breaker.adaptive_threshold import (
            AdaptiveThresholdManager,
        )

        manager = AdaptiveThresholdManager()
        threshold = manager.get_adjusted_threshold(emergency_level="LOCKDOWN")

        assert threshold.failure_threshold == float("inf")
        assert threshold.window_seconds == float("inf")
        assert threshold.is_lockdown is True

    def test_should_allow_auto_open_normal(self):
        """NORMAL 레벨에서 자동 OPEN 허용."""
        from baldur.services.circuit_breaker.adaptive_threshold import (
            AdaptiveThresholdManager,
        )

        manager = AdaptiveThresholdManager()

        with patch.object(
            manager, "get_current_emergency_level", return_value="NORMAL"
        ):
            allowed, reason = manager.should_allow_auto_open()

        assert allowed is True
        assert reason == ""

    def test_should_allow_auto_open_lockdown(self):
        """LOCKDOWN 레벨에서 자동 OPEN 금지."""
        from baldur.services.circuit_breaker.adaptive_threshold import (
            AdaptiveThresholdManager,
        )

        manager = AdaptiveThresholdManager()

        with patch.object(
            manager, "get_current_emergency_level", return_value="LOCKDOWN"
        ):
            allowed, reason = manager.should_allow_auto_open()

        assert allowed is False
        assert "LOCKDOWN" in reason

    def test_check_threshold_exceeded_normal(self):
        """임계값 초과 확인 - 초과 케이스."""
        import time

        from baldur.services.circuit_breaker.adaptive_threshold import (
            AdaptiveThresholdManager,
        )

        manager = AdaptiveThresholdManager()
        current_time = time.time()

        with patch.object(
            manager, "get_current_emergency_level", return_value="NORMAL"
        ):
            exceeded, threshold = manager.check_threshold_exceeded(
                failure_count=6,  # 5 이상이면 초과
                window_start_time=current_time - 30,  # 30초 전
                current_time=current_time,
            )

        assert exceeded is True
        assert threshold.failure_threshold == 5.0

    def test_check_threshold_exceeded_lockdown_never_exceeds(self):
        """LOCKDOWN에서는 절대 초과하지 않음."""
        import time

        from baldur.services.circuit_breaker.adaptive_threshold import (
            AdaptiveThresholdManager,
        )

        manager = AdaptiveThresholdManager()
        current_time = time.time()

        with patch.object(
            manager, "get_current_emergency_level", return_value="LOCKDOWN"
        ):
            exceeded, threshold = manager.check_threshold_exceeded(
                failure_count=1000,  # 아무리 많아도
                window_start_time=current_time - 30,
                current_time=current_time,
            )

        assert exceeded is False
        assert threshold.is_lockdown is True

    def test_disabled_policy_returns_base_values(self):
        """비활성화된 정책은 기본값 반환."""
        from baldur.services.circuit_breaker.adaptive_threshold import (
            AdaptiveThresholdManager,
            AdaptiveThresholdPolicy,
        )

        policy = AdaptiveThresholdPolicy(enabled=False)
        manager = AdaptiveThresholdManager(policy=policy)

        threshold = manager.get_adjusted_threshold(emergency_level="LOCKDOWN")

        # 비활성화면 LOCKDOWN이어도 기본값
        assert threshold.emergency_level == "DISABLED"
        assert threshold.is_lockdown is False


# =============================================================================
# Freeze Mode Tests
# =============================================================================


class TestFreezeMode:
    """Freeze Mode 기능 테스트 (freeze_mode.py)."""

    def test_freeze_mode_inactive_by_default(self):
        """기본 상태는 비활성화."""
        from baldur.services.circuit_breaker.freeze_mode import FreezeModeManager

        # 싱글톤 리셋을 위해 새 인스턴스 생성
        FreezeModeManager._instance = None
        manager = FreezeModeManager()
        manager._state.active = False  # 명시적 비활성화

        with patch.object(manager, "_is_lockdown", return_value=False):
            assert manager.is_active() is False

    def test_freeze_mode_activate(self):
        """수동 활성화 테스트."""
        from baldur.services.circuit_breaker.freeze_mode import FreezeModeManager

        FreezeModeManager._instance = None
        manager = FreezeModeManager()

        result = manager.activate(reason="Test activation", activated_by="test-user")

        assert result is True
        assert manager._state.active is True
        assert manager._state.reason == "Test activation"
        assert manager._state.activated_by == "test-user"

    def test_freeze_mode_auto_active_on_lockdown(self):
        """LOCKDOWN 상태에서 자동 활성화."""
        from baldur.services.circuit_breaker.freeze_mode import FreezeModeManager

        FreezeModeManager._instance = None
        manager = FreezeModeManager()
        manager._state.active = False  # 수동으로는 비활성화

        with patch.object(manager, "_is_lockdown", return_value=True):
            assert manager.is_active() is True

    def test_should_allow_state_change_manual_allowed_in_freeze(self):
        """Freeze Mode에서 수동 조작은 허용."""
        from baldur.services.circuit_breaker.freeze_mode import FreezeModeManager

        FreezeModeManager._instance = None
        manager = FreezeModeManager()
        manager._state.active = True

        with patch.object(manager, "_is_lockdown", return_value=False):
            allowed, reason = manager.should_allow_state_change(
                service_id="payment-api",
                new_state="OPEN",
                is_manual=True,
            )

        assert allowed is True

    def test_should_allow_state_change_auto_blocked_in_freeze(self):
        """Freeze Mode에서 자동 조작은 금지."""
        from baldur.services.circuit_breaker.freeze_mode import FreezeModeManager

        FreezeModeManager._instance = None
        manager = FreezeModeManager()
        manager._state.active = True

        allowed, reason = manager.should_allow_state_change(
            service_id="payment-api",
            new_state="OPEN",
            is_manual=False,
        )

        assert allowed is False
        assert "Freeze Mode" in reason

    def test_should_allow_canary_recovery_blocked_in_freeze(self):
        """Freeze Mode에서 Canary Recovery 금지."""
        from baldur.services.circuit_breaker.freeze_mode import FreezeModeManager

        FreezeModeManager._instance = None
        manager = FreezeModeManager()
        manager._state.active = True

        allowed, reason = manager.should_allow_canary_recovery("payment-api")

        assert allowed is False
        assert "Canary Recovery blocked" in reason

    def test_deactivate_blocked_during_lockdown(self):
        """LOCKDOWN 중에는 수동 비활성화 불가."""
        from baldur.services.circuit_breaker.freeze_mode import FreezeModeManager

        FreezeModeManager._instance = None
        manager = FreezeModeManager()
        manager._state.active = True

        with patch.object(manager, "_is_lockdown", return_value=True):
            result = manager.deactivate(reason="try to deactivate")

        assert result is False
        assert manager._state.active is True


# =============================================================================
# Panic Threshold Tests
# =============================================================================


class TestPanicThreshold:
    """Panic Threshold 기능 테스트 (panic_threshold.py)."""

    def test_panic_threshold_disabled(self):
        """비활성화 시 발동하지 않음."""
        from baldur.services.circuit_breaker.panic_threshold import (
            PanicThresholdConfig,
            PanicThresholdMonitor,
        )

        config = PanicThresholdConfig(enabled=False)
        monitor = PanicThresholdMonitor(config=config)

        result = monitor.check_panic_threshold()

        assert result.triggered is False
        assert "disabled" in result.reason.lower()

    def test_panic_threshold_below_threshold(self):
        """임계값 미만이면 발동하지 않음."""
        from baldur.services.circuit_breaker.panic_threshold import (
            PanicThresholdConfig,
            PanicThresholdMonitor,
        )

        config = PanicThresholdConfig(enabled=True, threshold_percent=70.0)
        monitor = PanicThresholdMonitor(config=config)

        # 50% OPEN (70% 미만)
        with patch.object(
            monitor,
            "_get_circuit_stats",
            return_value=(
                ["svc1", "svc2", "svc3"],  # 3 OPEN
                ["svc1", "svc2", "svc3", "svc4", "svc5", "svc6"],  # 6 total
            ),
        ):
            result = monitor.check_panic_threshold()

        assert result.triggered is False
        assert result.open_rate == 50.0

    def test_panic_threshold_insufficient_services(self):
        """최소 서비스 수 미만이면 발동하지 않음."""
        from baldur.services.circuit_breaker.panic_threshold import (
            PanicThresholdConfig,
            PanicThresholdMonitor,
        )

        config = PanicThresholdConfig(enabled=True)
        monitor = PanicThresholdMonitor(config=config)

        # 2개 서비스만 (최소 3개 필요)
        with patch.object(
            monitor,
            "_get_circuit_stats",
            return_value=(
                ["svc1", "svc2"],  # 2 OPEN
                ["svc1", "svc2"],  # 2 total (100% OPEN이지만 서비스 수 부족)
            ),
        ):
            result = monitor.check_panic_threshold()

        assert result.triggered is False
        assert "Insufficient services" in result.reason

    def test_panic_threshold_triggers_on_threshold_exceeded(self):
        """임계값 초과 시 발동."""
        from baldur.services.circuit_breaker.panic_threshold import (
            PanicThresholdConfig,
            PanicThresholdMonitor,
        )

        config = PanicThresholdConfig(
            enabled=True, threshold_percent=70.0, action="alert_only"
        )
        monitor = PanicThresholdMonitor(config=config)
        monitor._consecutive_triggers = 1  # 연속 감지 1회 충족

        # 80% OPEN (70% 초과)
        with patch.object(
            monitor,
            "_get_circuit_stats",
            return_value=(
                ["svc1", "svc2", "svc3", "svc4"],  # 4 OPEN
                ["svc1", "svc2", "svc3", "svc4", "svc5"],  # 5 total = 80%
            ),
        ):
            with patch.object(monitor, "_log_panic_audit"):
                with patch.object(monitor, "_notify_critical"):
                    result = monitor.check_panic_threshold()

        assert result.triggered is True
        assert result.open_rate == 80.0
        assert len(result.open_circuits) == 4

    def test_panic_threshold_requires_consecutive_triggers(self):
        """연속 감지 횟수 미충족 시 발동하지 않음."""
        from baldur.services.circuit_breaker.panic_threshold import (
            PanicThresholdConfig,
            PanicThresholdMonitor,
        )

        config = PanicThresholdConfig(enabled=True, threshold_percent=70.0)
        monitor = PanicThresholdMonitor(config=config)
        monitor._consecutive_triggers = 0  # 첫 번째 감지

        with patch.object(
            monitor,
            "_get_circuit_stats",
            return_value=(
                ["svc1", "svc2", "svc3", "svc4"],
                ["svc1", "svc2", "svc3", "svc4", "svc5"],
            ),
        ):
            result = monitor.check_panic_threshold()

        assert result.triggered is False
        assert "waiting for consecutive triggers" in result.reason.lower()

    def test_panic_threshold_result_fields(self):
        """PanicThresholdResult 필드 검증."""
        from baldur.services.circuit_breaker.panic_threshold import (
            PanicThresholdResult,
        )

        result = PanicThresholdResult(
            triggered=True,
            open_rate=75.0,
            open_count=3,
            total_count=4,
            open_circuits=["a", "b", "c"],
            action_taken="emergency_level_3_escalation",
            halted_systems=["replay", "canary_recovery"],
        )

        assert result.triggered is True
        assert result.open_rate == 75.0
        assert result.open_count == 3
        assert "replay" in result.halted_systems


# =============================================================================
# Integration Tests
# =============================================================================


class TestKillSwitchAdaptiveFreezeIntegration:
    """Kill Switch 및 Adaptive Freeze 기능 통합 테스트."""

    def test_adaptive_threshold_with_freeze_mode(self):
        """Adaptive Threshold와 Freeze Mode 연동."""
        from baldur.services.circuit_breaker.adaptive_threshold import (
            AdaptiveThresholdManager,
        )
        from baldur.services.circuit_breaker.freeze_mode import FreezeModeManager

        # Freeze Mode 활성화
        FreezeModeManager._instance = None
        freeze_manager = FreezeModeManager()
        freeze_manager.activate(reason="test")

        # Adaptive Threshold 확인
        threshold_manager = AdaptiveThresholdManager()

        with patch.object(
            threshold_manager, "get_current_emergency_level", return_value="LOCKDOWN"
        ):
            threshold = threshold_manager.get_adjusted_threshold()
            allowed, _ = threshold_manager.should_allow_auto_open()

        assert threshold.is_lockdown is True
        assert allowed is False

    def test_imports_work(self):
        """모든 모듈 import 확인."""
        from baldur.services.circuit_breaker import (
            # Adaptive Threshold
            AdaptiveThresholdManager,
            FreezeModeManager,
            PanicThresholdMonitor,
        )

        # 모든 import 성공
        assert AdaptiveThresholdManager is not None
        assert FreezeModeManager is not None
        assert PanicThresholdMonitor is not None


# =============================================================================
# Audit Helper Tests
# =============================================================================


class TestAuditHelpers:
    """Audit Helper 함수 테스트."""

    @pytest.fixture(autouse=True)
    def _require_pro(self):
        pytest.importorskip("baldur_pro")

    def test_log_kill_switch_override_audit(self):
        """Kill Switch Override Audit 기록."""
        from baldur_pro.services.audit import log_kill_switch_override_audit

        # WAL 기록 성공 확인
        result = log_kill_switch_override_audit(
            service_name="payment-api",
            action="force_open",
            reason="emergency recovery",
            controlled_by_id=123,
        )

        # WAL 시퀀스 또는 None 반환
        assert result is None or isinstance(result, int)

    def test_log_panic_threshold_audit(self):
        """Panic Threshold Audit 기록."""
        from baldur_pro.services.audit import log_panic_threshold_audit

        result = log_panic_threshold_audit(
            open_rate=75.0,
            threshold=70.0,
            open_count=3,
            total_count=4,
            open_circuits=["a", "b", "c"],
            action_taken="emergency_level_3_escalation",
        )

        assert result is None or isinstance(result, int)

    def test_log_freeze_mode_audit(self):
        """Freeze Mode Audit 기록."""
        from baldur_pro.services.audit import log_freeze_mode_audit

        result = log_freeze_mode_audit(
            active=True,
            reason="LOCKDOWN entry",
            activated_by="system",
        )

        assert result is None or isinstance(result, int)
