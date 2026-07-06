"""
CapacityReservationService Unit Tests.

Test Categories:
    A. Behavior — Singleton: 싱글톤 생성/리셋
    B. Behavior — initialize: 초기화, 이중 초기화 방지
    C. Behavior — register/cancel: 이벤트 등록/취소
    D. Behavior — start/stop: 스케줄러 라이프사이클
    E. Behavior — get_status: 상태 조회
    F. Behavior — Safety Valve: 스케줄러 내 Safety Valve 체크
"""

import threading
from datetime import UTC, datetime, timedelta

import pytest

from baldur.services.capacity_reservation.event_calendar import (
    EventStatus,
    ScheduledEvent,
)
from baldur.services.capacity_reservation.service import (
    CapacityReservationService,
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


# =============================================================================
# A. Behavior — Singleton
# =============================================================================


class TestCapacityReservationServiceSingletonBehavior:
    """싱글톤 생성/리셋 동작 검증."""

    def teardown_method(self):
        CapacityReservationService.reset()

    def test_singleton_returns_same_instance(self):
        """CapacityReservationService()는 동일 인스턴스를 반환."""
        first = CapacityReservationService()
        second = CapacityReservationService()
        assert first is second

    def test_reset_creates_new_instance(self):
        """reset() 후 새 인스턴스가 생성된다."""
        first = CapacityReservationService()
        first.initialize(settings=CapacityReservationSettings())
        CapacityReservationService.reset()
        second = CapacityReservationService()
        assert first is not second

    def test_concurrent_singleton_access_returns_same_instance(self):
        """멀티스레드에서 동시 접근해도 동일 인스턴스."""
        results = []

        def worker():
            results.append(CapacityReservationService())

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert all(r is results[0] for r in results)


# =============================================================================
# B. Behavior — initialize
# =============================================================================


class TestCapacityReservationServiceInitBehavior:
    """초기화 동작 검증."""

    def teardown_method(self):
        CapacityReservationService.reset()

    def test_initialize_creates_calendar_and_prewarmer(self):
        """initialize() 호출 후 calendar/pre_warmer 접근 가능."""
        svc = CapacityReservationService()
        svc.initialize(settings=CapacityReservationSettings())
        assert svc.calendar is not None
        assert svc.pre_warmer is not None

    def test_double_initialize_is_idempotent(self):
        """initialize() 2회 호출해도 재초기화되지 않음."""
        svc = CapacityReservationService()
        svc.initialize(settings=CapacityReservationSettings())
        cal1 = svc.calendar
        svc.initialize(settings=CapacityReservationSettings())
        assert svc.calendar is cal1

    def test_not_initialized_raises_runtime_error(self):
        """초기화 없이 메서드 호출 시 RuntimeError."""
        svc = CapacityReservationService()
        with pytest.raises(RuntimeError, match="not initialized"):
            svc.get_status()


# =============================================================================
# C. Behavior — register/cancel
# =============================================================================


class TestCapacityReservationServiceRegisterBehavior:
    """이벤트 등록/취소 동작 검증."""

    def teardown_method(self):
        CapacityReservationService.reset()

    def test_register_event_adds_to_calendar(self):
        """register_event() 호출 시 캘린더에 이벤트 추가."""
        svc = CapacityReservationService()
        svc.initialize(settings=CapacityReservationSettings())
        event = _make_event()
        svc.register_event(event)
        assert svc.calendar.get_event(event.event_id) is not None

    def test_register_exceeding_max_concurrent_raises(self):
        """ValueError when the concurrent active event limit is exceeded."""
        settings = CapacityReservationSettings(max_concurrent_events=1)
        svc = CapacityReservationService()
        svc.initialize(settings=settings)

        e1 = _make_event(event_id="e1")
        svc.register_event(e1)
        svc.calendar.update_status("e1", EventStatus.ACTIVE)

        with pytest.raises(ValueError, match="limit exceeded"):
            svc.register_event(_make_event(event_id="e2"))

    def test_cancel_nonexistent_returns_false(self):
        """존재하지 않는 이벤트 취소 시 False."""
        svc = CapacityReservationService()
        svc.initialize(settings=CapacityReservationSettings())
        assert svc.cancel_event("nonexistent") is False

    def test_cancel_existing_event_returns_true(self):
        """등록된 이벤트 취소 시 True."""
        svc = CapacityReservationService()
        svc.initialize(settings=CapacityReservationSettings())
        event = _make_event()
        svc.register_event(event)
        assert svc.cancel_event(event.event_id) is True


# =============================================================================
# D. Behavior — start/stop
# =============================================================================


class TestCapacityReservationServiceSchedulerBehavior:
    """스케줄러 라이프사이클 검증."""

    def teardown_method(self):
        CapacityReservationService.reset()

    def test_start_disabled_service_does_not_start_thread(self):
        """enabled=False이면 스케줄러 스레드 미시작."""
        svc = CapacityReservationService()
        svc.initialize(settings=CapacityReservationSettings(enabled=False))
        svc.start()
        assert svc._scheduler_thread is None

    def test_start_enabled_service_starts_daemon_thread(self):
        """enabled=True이면 daemon 스케줄러 스레드 시작."""
        svc = CapacityReservationService()
        svc.initialize(settings=CapacityReservationSettings(enabled=True))
        svc.start()
        assert svc._scheduler_thread is not None
        assert svc._scheduler_thread.is_alive()
        assert svc._scheduler_thread.daemon is True
        svc.stop()

    def test_stop_terminates_scheduler_thread(self):
        """stop() 호출 시 스케줄러 스레드 종료."""
        svc = CapacityReservationService()
        svc.initialize(settings=CapacityReservationSettings(enabled=True))
        svc.start()
        svc.stop()
        assert svc._scheduler_thread is None

    def test_double_start_does_not_create_second_thread(self):
        """start() 2회 호출해도 스레드가 추가 생성되지 않음."""
        svc = CapacityReservationService()
        svc.initialize(settings=CapacityReservationSettings(enabled=True))
        svc.start()
        thread1 = svc._scheduler_thread
        svc.start()
        assert svc._scheduler_thread is thread1
        svc.stop()


# =============================================================================
# E. Behavior — get_status
# =============================================================================


class TestCapacityReservationServiceStatusBehavior:
    """get_status 동작 검증."""

    def teardown_method(self):
        CapacityReservationService.reset()

    def test_get_status_contains_expected_keys(self):
        """get_status() 결과에 필수 키가 포함."""
        svc = CapacityReservationService()
        svc.initialize(settings=CapacityReservationSettings())
        status = svc.get_status()
        assert "enabled" in status
        assert "dry_run" in status
        assert "scheduler_running" in status
        assert "active_events" in status
        assert "active_adjustments" in status
        assert "safety_valve_active" in status

    def test_get_status_reflects_settings(self):
        """get_status()가 설정 값을 반영."""
        svc = CapacityReservationService()
        svc.initialize(
            settings=CapacityReservationSettings(enabled=True, dry_run=False)
        )
        status = svc.get_status()
        assert status["enabled"] is True
        assert status["dry_run"] is False
