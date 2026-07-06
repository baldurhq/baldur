"""
PreWarmer Unit Tests.

Test Categories:
    A. Contract: WarmUpResult/CoolDownResult 필드, STATE_KEY_GLOBAL_BASELINE
    B. Behavior — warm_up: dry-run, 정상 워밍, Global Baseline 캡처, rollback
    C. Behavior — cool_down: Re-evaluation, Baseline 복원
    D. Behavior — Safety Valve: 발동/복구/Flapping 방지
    E. Behavior — initialize: 고아 Baseline 복원, 활성 이벤트 재개
    F. Behavior — Dependency Interaction: EventBus emit 검증
"""

import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

from baldur.services.capacity_reservation.event_calendar import (
    EventCalendar,
    EventStatus,
    ScheduledEvent,
)
from baldur.services.capacity_reservation.pre_warmer import (
    STATE_KEY_GLOBAL_BASELINE,
    AdjustmentRecord,
    CoolDownResult,
    PreWarmer,
    WarmUpResult,
)
from baldur.settings.capacity_reservation import CapacityReservationSettings

# ─── Helpers ─────────────────────────────────────────────────────────────────


def _future(minutes: int = 30) -> datetime:
    return datetime.now(UTC) + timedelta(minutes=minutes)


def _make_event(**kwargs) -> ScheduledEvent:
    defaults = {
        "name": "test-event",
        "start_time": _future(30),
        "end_time": _future(90),
    }
    defaults.update(kwargs)
    return ScheduledEvent(**defaults)


@dataclass
class MockRateControllerSettings:
    min_rate_per_second: float = 10.0


@dataclass
class MockBulkheadState:
    max_concurrent: int = 50
    active_count: int = 0
    waiting_count: int = 0
    rejected_count: int = 0


class MockRateController:
    def __init__(self):
        self._settings = MockRateControllerSettings()


class MockBulkhead:
    def __init__(self):
        self._state = MockBulkheadState()

    def get_state(self):
        return self._state


def _make_pre_warmer(
    dry_run=False,
    with_rate_controller=True,
    with_bulkhead=True,
    with_event_bus=False,
    with_metrics_provider=False,
    with_recovery_gate=False,
    with_state_backend=False,
    calendar=None,
    **settings_kwargs,
) -> tuple:
    """PreWarmer와 관련 Mock 객체를 생성."""
    settings = CapacityReservationSettings(dry_run=dry_run, **settings_kwargs)

    if calendar is None:
        calendar = EventCalendar(settings=settings)

    rate_controller = MockRateController() if with_rate_controller else None
    bulkhead = MockBulkhead() if with_bulkhead else None
    degradation = MagicMock() if not dry_run else None
    event_bus = MagicMock() if with_event_bus else None
    metrics_provider = MagicMock() if with_metrics_provider else None
    recovery_gate = MagicMock() if with_recovery_gate else None
    state_backend = MagicMock() if with_state_backend else None

    pw = PreWarmer(
        calendar=calendar,
        rate_controller=rate_controller,
        pool_watchdog=None,
        bulkhead=bulkhead,
        graceful_degradation=degradation,
        event_bus=event_bus,
        metrics_provider=metrics_provider,
        recovery_gate=recovery_gate,
        state_backend=state_backend,
        settings=settings,
    )

    return pw, {
        "calendar": calendar,
        "rate_controller": rate_controller,
        "bulkhead": bulkhead,
        "degradation": degradation,
        "event_bus": event_bus,
        "metrics_provider": metrics_provider,
        "recovery_gate": recovery_gate,
        "state_backend": state_backend,
        "settings": settings,
    }


# =============================================================================
# A. Contract Tests
# =============================================================================


class TestPreWarmerDataClassesContract:
    """WarmUpResult/CoolDownResult 필드 계약 검증."""

    def test_warmup_result_fields(self):
        """WarmUpResult에 필수 필드 존재."""
        r = WarmUpResult(event_id="e1", success=True)
        assert r.event_id == "e1"
        assert r.success is True
        assert r.adjustments == []
        assert r.errors == []
        assert r.duration_seconds == 0.0

    def test_cooldown_result_fields(self):
        """CoolDownResult에 필수 필드 존재."""
        r = CoolDownResult(event_id="e1", success=True)
        assert r.event_id == "e1"
        assert r.success is True
        assert r.restored == []
        assert r.errors == []

    def test_adjustment_record_defaults(self):
        """AdjustmentRecord 기본값: applied=False."""
        r = AdjustmentRecord(target="test", original_value=1, adjusted_value=2)
        assert r.applied is False

    def test_global_baseline_state_key_contract(self):
        """STATE_KEY_GLOBAL_BASELINE 값 계약."""
        assert STATE_KEY_GLOBAL_BASELINE == "capacity_reservation:global_baseline"


# =============================================================================
# B. Behavior — warm_up
# =============================================================================


class TestPreWarmerWarmUpBehavior:
    """warm_up 동작 검증."""

    def test_dry_run_returns_success_without_adjustments(self):
        """dry_run=True이면 실제 조정 없이 성공 반환."""
        pw, _ = _make_pre_warmer(dry_run=True)
        event = _make_event()
        result = pw.warm_up(event)
        assert result.success is True
        assert result.adjustments == []

    def test_warm_up_captures_global_baseline_on_first_call(self):
        """최초 warm_up 시 Global Baseline 캡처."""
        pw, deps = _make_pre_warmer()
        event = _make_event()
        pw.warm_up(event)
        assert pw._global_baseline is not None
        assert "min_rate_per_second" in pw._global_baseline

    def test_warm_up_does_not_overwrite_existing_baseline(self):
        """두 번째 warm_up에서 기존 Baseline 덮어쓰지 않음."""
        pw, deps = _make_pre_warmer()
        e1 = _make_event(event_id="e1")
        e2 = _make_event(event_id="e2")

        # Given — 첫 워밍으로 Baseline 캡처
        cal = deps["calendar"]
        cal.register(e1)
        cal.update_status("e1", EventStatus.ACTIVE)
        pw.warm_up(e1)
        original_baseline = dict(pw._global_baseline)

        # When — rate를 변경한 뒤 두 번째 워밍
        deps["rate_controller"]._settings.min_rate_per_second = 99.0
        cal.register(e2)
        cal.update_status("e2", EventStatus.ACTIVE)
        pw.warm_up(e2)

        # Then — Baseline은 최초 캡처값 유지
        assert (
            pw._global_baseline["min_rate_per_second"]
            == original_baseline["min_rate_per_second"]
        )

    def test_warm_up_adjusts_rate_controller(self):
        """warm_up 후 rate_controller.min_rate_per_second가 배율 적용."""
        pw, deps = _make_pre_warmer()
        event = _make_event(expected_rps_multiplier=3.0)
        cal = deps["calendar"]
        cal.register(event)
        cal.update_status(event.event_id, EventStatus.ACTIVE)

        original_rate = deps["rate_controller"]._settings.min_rate_per_second
        pw.warm_up(event)

        expected = original_rate * 3.0
        assert deps["rate_controller"]._settings.min_rate_per_second == expected

    def test_warm_up_adjusts_bulkhead(self):
        """warm_up 후 bulkhead.max_concurrent가 증가."""
        pw, deps = _make_pre_warmer()
        event = _make_event(bulkhead_extra_permits=30)
        cal = deps["calendar"]
        cal.register(event)
        cal.update_status(event.event_id, EventStatus.ACTIVE)

        original_max = deps["bulkhead"]._state.max_concurrent
        pw.warm_up(event)

        assert deps["bulkhead"]._state.max_concurrent == original_max + 30

    def test_warm_up_persists_baseline_to_state_backend(self):
        """warm_up 시 Global Baseline이 StateBackend에 저장됨."""
        pw, deps = _make_pre_warmer(with_state_backend=True)
        event = _make_event()
        cal = deps["calendar"]
        cal.register(event)
        cal.update_status(event.event_id, EventStatus.ACTIVE)

        pw.warm_up(event)
        deps["state_backend"].set.assert_called()

    def test_warm_up_returns_applied_adjustments(self):
        """warm_up 결과에 applied=True인 조정 목록 포함."""
        pw, deps = _make_pre_warmer()
        event = _make_event()
        cal = deps["calendar"]
        cal.register(event)
        cal.update_status(event.event_id, EventStatus.ACTIVE)

        result = pw.warm_up(event)
        applied = [a for a in result.adjustments if a.applied]
        assert len(applied) > 0


# =============================================================================
# C. Behavior — cool_down
# =============================================================================


class TestPreWarmerCoolDownBehavior:
    """cool_down 동작 검증."""

    def test_cool_down_dry_run_returns_success(self):
        """dry_run 모드에서 cool_down은 실제 복원 없이 성공."""
        pw, _ = _make_pre_warmer(dry_run=True)
        event = _make_event()
        result = pw.cool_down(event)
        assert result.success is True

    def test_cool_down_last_event_restores_baseline(self):
        """마지막 이벤트 종료 시 Global Baseline으로 복원."""
        pw, deps = _make_pre_warmer()
        event = _make_event()
        cal = deps["calendar"]
        cal.register(event)
        cal.update_status(event.event_id, EventStatus.ACTIVE)

        # Given — warm_up으로 조정
        original_rate = deps["rate_controller"]._settings.min_rate_per_second
        original_bulkhead = deps["bulkhead"]._state.max_concurrent
        pw.warm_up(event)

        # When — cool_down (마지막 이벤트)
        cal.update_status(event.event_id, EventStatus.COOLING_DOWN)
        pw.cool_down(event)

        # Then — 원래 값으로 복원
        assert deps["rate_controller"]._settings.min_rate_per_second == original_rate
        assert deps["bulkhead"]._state.max_concurrent == original_bulkhead

    def test_cool_down_clears_global_baseline_when_no_remaining(self):
        """마지막 이벤트 종료 시 Global Baseline이 None으로 리셋."""
        pw, deps = _make_pre_warmer()
        event = _make_event()
        cal = deps["calendar"]
        cal.register(event)
        cal.update_status(event.event_id, EventStatus.ACTIVE)
        pw.warm_up(event)

        cal.update_status(event.event_id, EventStatus.COOLING_DOWN)
        pw.cool_down(event)
        assert pw._global_baseline is None

    def test_cool_down_with_remaining_events_does_reevaluation(self):
        """남은 이벤트가 있으면 Re-evaluation (MAX 재계산)."""
        pw, deps = _make_pre_warmer()
        cal = deps["calendar"]
        e1 = _make_event(event_id="e1", expected_rps_multiplier=2.0)
        e2 = _make_event(event_id="e2", expected_rps_multiplier=4.0)
        cal.register(e1)
        cal.register(e2)
        cal.update_status("e1", EventStatus.ACTIVE)
        cal.update_status("e2", EventStatus.ACTIVE)

        original_rate = deps["rate_controller"]._settings.min_rate_per_second
        pw.warm_up(e1)
        pw.warm_up(e2)

        # When — e2 종료 (e1은 아직 활성)
        cal.update_status("e2", EventStatus.COOLING_DOWN)
        pw.cool_down(e2)

        # Then — e1의 배율(2.0)로 재계산됨
        expected = original_rate * 2.0
        assert deps["rate_controller"]._settings.min_rate_per_second == expected

    def test_cool_down_deletes_baseline_from_backend_when_last(self):
        """마지막 이벤트 종료 시 StateBackend에서 Baseline 삭제."""
        pw, deps = _make_pre_warmer(with_state_backend=True)
        event = _make_event()
        cal = deps["calendar"]
        cal.register(event)
        cal.update_status(event.event_id, EventStatus.ACTIVE)
        pw.warm_up(event)

        cal.update_status(event.event_id, EventStatus.COOLING_DOWN)
        pw.cool_down(event)
        deps["state_backend"].delete.assert_called_with(STATE_KEY_GLOBAL_BASELINE)


# =============================================================================
# D. Behavior — Safety Valve
# =============================================================================


class TestPreWarmerSafetyValveBehavior:
    """Safety Valve 발동/복구/Flapping 방지 검증."""

    def test_check_safety_valve_true_when_cpu_exceeds_threshold(self):
        """CPU가 임계치 초과 시 True 반환."""
        pw, deps = _make_pre_warmer(with_metrics_provider=True)
        deps["metrics_provider"].get_cpu_usage.return_value = 0.96
        deps["metrics_provider"].get_error_rate.return_value = 0.01
        assert pw.check_safety_valve() is True

    def test_check_safety_valve_true_when_error_rate_exceeds_threshold(self):
        """Error Rate이 임계치 초과 시 True 반환."""
        pw, deps = _make_pre_warmer(with_metrics_provider=True)
        deps["metrics_provider"].get_cpu_usage.return_value = 0.5
        deps["metrics_provider"].get_error_rate.return_value = 0.15
        assert pw.check_safety_valve() is True

    def test_check_safety_valve_false_when_within_limits(self):
        """CPU와 Error Rate 모두 정상 범위이면 False."""
        pw, deps = _make_pre_warmer(with_metrics_provider=True)
        deps["metrics_provider"].get_cpu_usage.return_value = 0.7
        deps["metrics_provider"].get_error_rate.return_value = 0.05
        assert pw.check_safety_valve() is False

    def test_check_safety_valve_false_when_no_provider(self):
        """MetricsProvider가 없으면 항상 False."""
        pw, _ = _make_pre_warmer(with_metrics_provider=False)
        assert pw.check_safety_valve() is False

    def test_emergency_override_activates_safety_valve(self):
        """emergency_override() 호출 후 safety_valve_active == True."""
        pw, deps = _make_pre_warmer()
        pw.emergency_override()
        assert pw.safety_valve_active is True

    def test_safety_valve_recovery_blocked_before_min_hold(self):
        """min_hold_seconds 경과 전에는 복구 차단."""
        pw, deps = _make_pre_warmer(
            with_metrics_provider=True,
            with_recovery_gate=True,
            safety_valve_min_hold_seconds=120,
        )
        pw._safety_valve_activated_at = time.monotonic()
        assert pw.check_safety_valve_recovery() is False

    def test_safety_valve_recovery_blocked_when_still_overloaded(self):
        """min_hold 경과해도 메트릭이 여전히 초과이면 복구 차단."""
        pw, deps = _make_pre_warmer(
            with_metrics_provider=True,
            with_recovery_gate=True,
            safety_valve_min_hold_seconds=30,
        )
        pw._safety_valve_activated_at = time.monotonic() - 200

        deps["recovery_gate"].check_recovery_allowed.return_value = (True, "ok")
        deps["metrics_provider"].get_cpu_usage.return_value = 0.97
        deps["metrics_provider"].get_error_rate.return_value = 0.01

        assert pw.check_safety_valve_recovery() is False

    def test_safety_valve_recovery_succeeds_when_all_conditions_met(self):
        """min_hold 경과 + RecoveryGate 허용 + 메트릭 정상 → 복구 성공."""
        pw, deps = _make_pre_warmer(
            with_metrics_provider=True,
            with_recovery_gate=True,
            safety_valve_min_hold_seconds=30,
        )
        pw._safety_valve_activated_at = time.monotonic() - 200

        deps["recovery_gate"].check_recovery_allowed.return_value = (True, "ok")
        deps["metrics_provider"].get_cpu_usage.return_value = 0.6
        deps["metrics_provider"].get_error_rate.return_value = 0.02

        assert pw.check_safety_valve_recovery() is True
        assert pw.safety_valve_active is False

    def test_safety_valve_not_active_by_default(self):
        """초기 상태에서 safety_valve_active == False."""
        pw, _ = _make_pre_warmer()
        assert pw.safety_valve_active is False


# =============================================================================
# E. Behavior — initialize
# =============================================================================


class TestPreWarmerInitializeBehavior:
    """고아 Baseline 복원 및 활성 이벤트 재개 검증."""

    def test_orphan_baseline_restored_when_no_active_events(self):
        """활성 이벤트 없이 Baseline만 남아있으면 복원 + 삭제."""
        pw, deps = _make_pre_warmer(with_state_backend=True)
        deps["state_backend"].get.return_value = {
            "min_rate_per_second": 10.0,
            "bulkhead_max_concurrent": 50,
        }

        pw.initialize()

        deps["state_backend"].delete.assert_called_with(STATE_KEY_GLOBAL_BASELINE)
        assert deps["rate_controller"]._settings.min_rate_per_second == 10.0

    def test_baseline_resumed_when_active_events_exist(self):
        """활성 이벤트가 있는 상태에서 Baseline이 남아있으면 재개."""
        pw, deps = _make_pre_warmer(with_state_backend=True)
        cal = deps["calendar"]
        event = _make_event()
        cal.register(event)
        cal.update_status(event.event_id, EventStatus.ACTIVE)

        saved_baseline = {"min_rate_per_second": 10.0}
        deps["state_backend"].get.return_value = saved_baseline

        pw.initialize()

        assert pw._global_baseline == saved_baseline

    def test_initialize_without_backend_does_nothing(self):
        """StateBackend가 없으면 initialize()는 아무것도 하지 않음."""
        pw, _ = _make_pre_warmer(with_state_backend=False)
        pw.initialize()


# =============================================================================
# F. Behavior — EventBus Interaction
# =============================================================================


class TestPreWarmerEventBusInteractionBehavior:
    """EventBus emit 호출 검증."""

    def test_warm_up_publishes_event_started(self):
        """warm_up 시 SCHEDULED_EVENT_STARTED 이벤트 발행."""
        pw, deps = _make_pre_warmer(with_event_bus=True)
        event = _make_event()
        cal = deps["calendar"]
        cal.register(event)
        cal.update_status(event.event_id, EventStatus.ACTIVE)

        pw.warm_up(event)

        deps["event_bus"].emit.assert_called_once()
        call_args = deps["event_bus"].emit.call_args
        from baldur.services.event_bus.bus import EventType

        assert call_args[0][0] == EventType.SCHEDULED_EVENT_STARTED

    def test_cool_down_publishes_event_ended(self):
        """cool_down 시 SCHEDULED_EVENT_ENDED 이벤트 발행."""
        pw, deps = _make_pre_warmer(with_event_bus=True)
        event = _make_event()
        cal = deps["calendar"]
        cal.register(event)
        cal.update_status(event.event_id, EventStatus.ACTIVE)
        pw.warm_up(event)
        deps["event_bus"].emit.reset_mock()

        cal.update_status(event.event_id, EventStatus.COOLING_DOWN)
        pw.cool_down(event)

        deps["event_bus"].emit.assert_called_once()
        call_args = deps["event_bus"].emit.call_args
        from baldur.services.event_bus.bus import EventType

        assert call_args[0][0] == EventType.SCHEDULED_EVENT_ENDED


# =============================================================================
# G. Behavior — Rollback
# =============================================================================


class TestPreWarmerRollbackBehavior:
    """warm_up 실패 시 rollback 동작 검증."""

    def test_warm_up_rollback_on_event_bus_error_restores_baseline(self):
        """EventBus publish 실패 시 조정이 rollback됨."""
        pw, deps = _make_pre_warmer(with_event_bus=True)
        deps["event_bus"].emit.side_effect = RuntimeError("bus error")

        event = _make_event()
        cal = deps["calendar"]
        cal.register(event)
        cal.update_status(event.event_id, EventStatus.ACTIVE)

        original_rate = deps["rate_controller"]._settings.min_rate_per_second
        result = pw.warm_up(event)

        assert result.success is False
        assert deps["rate_controller"]._settings.min_rate_per_second == original_rate
