"""
EventCalendar Unit Tests.

Test Categories:
    A. Contract: ScheduledEvent 기본값, EventStatus enum 계약값
    B. Behavior — Registration: 등록/취소/조회/중복ID/과거시간 검증
    C. Behavior — Scheduling: get_needs_warmup, get_needs_cooldown, get_upcoming
    D. Behavior — Multipliers: MAX 병합 + Settings cap
    E. Behavior — Serialization: to_dict/from_dict 라운드트립
    F. Behavior — StateBackend: 영속화 및 Pull 초기화
    G. Behavior — Thread Safety: 동시 등록
"""

import threading
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from baldur.services.capacity_reservation.event_calendar import (
    STATE_KEY_EVENTS,
    EventCalendar,
    EventStatus,
    ScheduledEvent,
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


def _make_calendar(**kwargs) -> EventCalendar:
    settings = CapacityReservationSettings()
    return EventCalendar(settings=settings, **kwargs)


# =============================================================================
# A. Contract Tests
# =============================================================================


class TestScheduledEventContract:
    """ScheduledEvent 기본값 계약 검증."""

    def test_expected_rps_multiplier_default(self):
        """expected_rps_multiplier 기본값: 2.0."""
        event = _make_event()
        assert event.expected_rps_multiplier == 2.0

    def test_pool_multiplier_default(self):
        """pool_multiplier 기본값: 1.5."""
        event = _make_event()
        assert event.pool_multiplier == 1.5

    def test_bulkhead_extra_permits_default(self):
        """bulkhead_extra_permits 기본값: 50."""
        event = _make_event()
        assert event.bulkhead_extra_permits == 50

    def test_suppress_degradation_default_is_true(self):
        """suppress_degradation 기본값: True."""
        event = _make_event()
        assert event.suppress_degradation is True

    def test_warmup_minutes_default(self):
        """warmup_minutes 기본값: 5."""
        event = _make_event()
        assert event.warmup_minutes == 5

    def test_default_status_is_pending(self):
        """기본 status: PENDING."""
        event = _make_event()
        assert event.status == EventStatus.PENDING


class TestEventStatusContract:
    """EventStatus enum 값 계약 검증."""

    def test_status_values(self):
        """EventStatus enum 값 6개."""
        assert EventStatus.PENDING.value == "pending"
        assert EventStatus.WARMING.value == "warming"
        assert EventStatus.ACTIVE.value == "active"
        assert EventStatus.COOLING_DOWN.value == "cooling_down"
        assert EventStatus.COMPLETED.value == "completed"
        assert EventStatus.CANCELLED.value == "cancelled"
        assert len(EventStatus) == 6


class TestEffectiveMultipliersNoActiveEventsContract:
    """활성 이벤트 없을 때 EffectiveMultipliers 기본값 계약."""

    def test_no_active_returns_neutral_multipliers(self):
        """활성 이벤트 없으면 rate=1.0, pool=1.0, permits=0, suppress=False."""
        cal = _make_calendar()
        m = cal.get_effective_multipliers()
        assert m.rate_multiplier == 1.0
        assert m.pool_multiplier == 1.0
        assert m.bulkhead_extra_permits == 0
        assert m.suppress_degradation is False
        assert m.source_event_ids == []


# =============================================================================
# B. Behavior — Registration
# =============================================================================


class TestEventCalendarRegistrationBehavior:
    """이벤트 등록/취소/조회 동작 검증."""

    def test_register_adds_event(self):
        """이벤트 등록 후 get_event로 조회 가능."""
        cal = _make_calendar()
        event = _make_event()
        cal.register(event)
        assert cal.get_event(event.event_id) is event

    def test_register_past_start_time_raises_value_error(self):
        """시작 시간이 과거이면 ValueError."""
        cal = _make_calendar()
        event = _make_event(start_time=datetime.now(UTC) - timedelta(hours=1))
        with pytest.raises(ValueError, match="past"):
            cal.register(event)

    def test_register_end_before_start_raises_value_error(self):
        """종료 시간이 시작 시간보다 빠르면 ValueError."""
        cal = _make_calendar()
        start = _future(60)
        event = _make_event(start_time=start, end_time=start - timedelta(minutes=10))
        with pytest.raises(ValueError, match="End time"):
            cal.register(event)

    def test_register_duplicate_id_raises_value_error(self):
        """동일 event_id 중복 등록 시 ValueError."""
        cal = _make_calendar()
        event = _make_event(event_id="dup-001")
        cal.register(event)
        event2 = _make_event(event_id="dup-001")
        with pytest.raises(ValueError, match="Duplicate"):
            cal.register(event2)

    def test_cancel_existing_event_returns_true(self):
        """존재하는 이벤트 취소 시 True 반환."""
        cal = _make_calendar()
        event = _make_event()
        cal.register(event)
        assert cal.cancel(event.event_id) is True

    def test_cancel_sets_status_to_cancelled(self):
        """취소 후 상태가 CANCELLED로 변경."""
        cal = _make_calendar()
        event = _make_event()
        cal.register(event)
        cal.cancel(event.event_id)
        assert cal.get_event(event.event_id).status == EventStatus.CANCELLED

    def test_cancel_nonexistent_returns_false(self):
        """존재하지 않는 이벤트 취소 시 False 반환."""
        cal = _make_calendar()
        assert cal.cancel("nonexistent-id") is False


# =============================================================================
# C. Behavior — Scheduling
# =============================================================================


class TestEventCalendarSchedulingBehavior:
    """워밍/쿨다운 스케줄링 동작 검증."""

    def test_get_needs_warmup_returns_events_past_warmup_time(self):
        """워밍 시각이 도달한 PENDING 이벤트를 반환."""
        cal = _make_calendar()
        event = _make_event(start_time=_future(3), warmup_minutes=5)
        cal.register(event)
        result = cal.get_needs_warmup()
        assert len(result) == 1
        assert result[0].event_id == event.event_id

    def test_get_needs_warmup_excludes_non_pending(self):
        """PENDING이 아닌 이벤트는 반환하지 않음."""
        cal = _make_calendar()
        event = _make_event(start_time=_future(3), warmup_minutes=5)
        cal.register(event)
        cal.update_status(event.event_id, EventStatus.ACTIVE)
        assert cal.get_needs_warmup() == []

    def test_get_needs_cooldown_returns_ended_active_events(self):
        """종료 시각 + grace period 경과한 ACTIVE 이벤트를 반환."""
        cal = _make_calendar()
        grace = cal._settings.cooldown_grace_period_seconds
        event = _make_event(
            start_time=datetime.now(UTC) - timedelta(hours=1),
            end_time=datetime.now(UTC) - timedelta(seconds=grace + 60),
        )
        event.status = EventStatus.PENDING
        cal._events[event.event_id] = event
        cal.update_status(event.event_id, EventStatus.ACTIVE)
        result = cal.get_needs_cooldown()
        assert len(result) == 1

    def test_get_needs_cooldown_respects_grace_period(self):
        """종료 직후(grace period 미경과) ACTIVE 이벤트는 반환하지 않음."""
        cal = _make_calendar()
        event = _make_event(
            start_time=datetime.now(UTC) - timedelta(hours=1),
            end_time=datetime.now(UTC) - timedelta(minutes=1),
        )
        event.status = EventStatus.PENDING
        cal._events[event.event_id] = event
        cal.update_status(event.event_id, EventStatus.ACTIVE)
        result = cal.get_needs_cooldown()
        assert len(result) == 0

    def test_get_active_includes_warming_and_active(self):
        """ACTIVE와 WARMING 상태 모두 반환."""
        cal = _make_calendar()
        e1 = _make_event(event_id="e1")
        e2 = _make_event(event_id="e2")
        cal.register(e1)
        cal.register(e2)
        cal.update_status("e1", EventStatus.ACTIVE)
        cal.update_status("e2", EventStatus.WARMING)
        active = cal.get_active()
        assert len(active) == 2

    def test_is_event_period_true_when_active(self):
        """ACTIVE 이벤트가 있으면 is_event_period() == True."""
        cal = _make_calendar()
        event = _make_event()
        cal.register(event)
        cal.update_status(event.event_id, EventStatus.ACTIVE)
        assert cal.is_event_period() is True

    def test_is_event_period_false_when_no_active(self):
        """ACTIVE 이벤트가 없으면 is_event_period() == False."""
        cal = _make_calendar()
        assert cal.is_event_period() is False

    def test_remove_completed_clears_finished_events(self):
        """완료/취소 이벤트가 정리됨."""
        cal = _make_calendar()
        e1 = _make_event(event_id="e1")
        e2 = _make_event(event_id="e2")
        cal.register(e1)
        cal.register(e2)
        cal.update_status("e1", EventStatus.COMPLETED)
        cal.update_status("e2", EventStatus.CANCELLED)
        removed = cal.remove_completed()
        assert removed == 2
        assert cal.get_event("e1") is None
        assert cal.get_event("e2") is None

    def test_get_upcoming_filters_by_warmup_time(self):
        """within_minutes 내 워밍 시작 시각의 PENDING 이벤트만 반환."""
        cal = _make_calendar()
        soon = _make_event(event_id="soon", start_time=_future(10), warmup_minutes=5)
        far = _make_event(
            event_id="far",
            start_time=_future(120),
            end_time=_future(180),
            warmup_minutes=5,
        )
        cal.register(soon)
        cal.register(far)
        result = cal.get_upcoming(within_minutes=60)
        assert len(result) == 1
        assert result[0].event_id == "soon"


# =============================================================================
# D. Behavior — Multipliers (MAX 병합 + Settings cap)
# =============================================================================


class TestEffectiveMultipliersBehavior:
    """활성 이벤트 MAX 병합 + Settings cap 적용 동작 검증."""

    def test_single_active_event_uses_event_multipliers(self):
        """단일 이벤트: 이벤트 배율 그대로 적용 (Settings cap 이내)."""
        cal = _make_calendar()
        event = _make_event(
            expected_rps_multiplier=3.0,
            pool_multiplier=2.0,
            bulkhead_extra_permits=80,
        )
        cal.register(event)
        cal.update_status(event.event_id, EventStatus.ACTIVE)

        m = cal.get_effective_multipliers()
        assert m.rate_multiplier == 3.0
        assert m.pool_multiplier == 2.0
        assert m.bulkhead_extra_permits == 80

    def test_overlapping_events_uses_max_strategy(self):
        """겹치는 이벤트: MAX 전략으로 배율 병합."""
        cal = _make_calendar()
        e1 = _make_event(
            event_id="e1",
            expected_rps_multiplier=2.0,
            pool_multiplier=1.5,
            bulkhead_extra_permits=30,
            suppress_degradation=False,
        )
        e2 = _make_event(
            event_id="e2",
            expected_rps_multiplier=4.0,
            pool_multiplier=2.5,
            bulkhead_extra_permits=60,
            suppress_degradation=True,
        )
        cal.register(e1)
        cal.register(e2)
        cal.update_status("e1", EventStatus.ACTIVE)
        cal.update_status("e2", EventStatus.ACTIVE)

        m = cal.get_effective_multipliers()
        assert m.rate_multiplier == 4.0
        assert m.pool_multiplier == 2.5
        assert m.bulkhead_extra_permits == 60
        assert m.suppress_degradation is True
        assert set(m.source_event_ids) == {"e1", "e2"}

    def test_settings_cap_limits_multipliers(self):
        """이벤트 배율이 Settings cap을 초과하면 cap 적용."""
        settings = CapacityReservationSettings(
            max_rate_multiplier=3.0,
            max_pool_multiplier=2.0,
            max_bulkhead_extra_permits=50,
        )
        cal = EventCalendar(settings=settings)
        event = _make_event(
            expected_rps_multiplier=10.0,
            pool_multiplier=5.0,
            bulkhead_extra_permits=200,
        )
        cal.register(event)
        cal.update_status(event.event_id, EventStatus.ACTIVE)

        m = cal.get_effective_multipliers()
        assert m.rate_multiplier == settings.max_rate_multiplier
        assert m.pool_multiplier == settings.max_pool_multiplier
        assert m.bulkhead_extra_permits == settings.max_bulkhead_extra_permits

    def test_suppress_degradation_any_true_wins(self):
        """suppress_degradation은 ANY(하나라도 True이면 True)."""
        cal = _make_calendar()
        e1 = _make_event(event_id="e1", suppress_degradation=False)
        e2 = _make_event(event_id="e2", suppress_degradation=True)
        cal.register(e1)
        cal.register(e2)
        cal.update_status("e1", EventStatus.ACTIVE)
        cal.update_status("e2", EventStatus.ACTIVE)

        m = cal.get_effective_multipliers()
        assert m.suppress_degradation is True


# =============================================================================
# E. Behavior — Serialization Round-trip
# =============================================================================


class TestScheduledEventSerializationBehavior:
    """ScheduledEvent 직렬화 왕복 검증."""

    def test_round_trip_preserves_all_fields(self):
        """to_dict → from_dict 왕복 시 모든 필드가 보존된다."""
        original = _make_event(
            name="Flash Sale",
            expected_rps_multiplier=3.5,
            pool_multiplier=2.0,
            bulkhead_extra_permits=70,
            suppress_degradation=False,
            warmup_minutes=10,
            tags=["flash", "sale"],
        )
        serialized = original.to_dict()
        restored = ScheduledEvent.from_dict(serialized)

        assert restored.name == original.name
        assert restored.start_time == original.start_time
        assert restored.end_time == original.end_time
        assert restored.expected_rps_multiplier == original.expected_rps_multiplier
        assert restored.pool_multiplier == original.pool_multiplier
        assert restored.bulkhead_extra_permits == original.bulkhead_extra_permits
        assert restored.suppress_degradation == original.suppress_degradation
        assert restored.warmup_minutes == original.warmup_minutes
        assert restored.tags == original.tags
        assert restored.event_id == original.event_id
        assert restored.status == original.status

    def test_to_dict_contains_expected_keys(self):
        """직렬화된 dict에 모든 필수 키가 존재."""
        event = _make_event()
        data = event.to_dict()
        expected_keys = {
            "name",
            "start_time",
            "end_time",
            "expected_rps_multiplier",
            "pool_multiplier",
            "bulkhead_extra_permits",
            "suppress_degradation",
            "warmup_minutes",
            "tags",
            "event_id",
            "status",
        }
        assert set(data.keys()) == expected_keys

    def test_to_event_context_has_scheduled_event_flag(self):
        """to_event_context() 결과에 scheduled_event=True 포함."""
        event = _make_event()
        ctx = event.to_event_context()
        assert ctx["scheduled_event"] is True
        assert ctx["event_id"] == event.event_id


# =============================================================================
# F. Behavior — StateBackend
# =============================================================================


class TestEventCalendarStateBackendBehavior:
    """StateBackend 영속화 및 Pull 초기화 검증."""

    def test_register_persists_to_backend(self):
        """이벤트 등록 시 StateBackend.set()이 호출된다."""
        backend = MagicMock()
        cal = _make_calendar(state_backend=backend)
        event = _make_event()
        cal.register(event)
        backend.set.assert_called()
        call_args = backend.set.call_args
        assert call_args[0][0] == STATE_KEY_EVENTS

    def test_initialize_loads_from_backend(self):
        """initialize() 호출 시 StateBackend.get()으로 이벤트 로드."""
        # Given
        event = _make_event(event_id="saved-1")
        saved_data = {event.event_id: event.to_dict()}
        backend = MagicMock()
        backend.get.return_value = saved_data

        # When
        cal = _make_calendar(state_backend=backend)
        cal.initialize()

        # Then
        assert cal.get_event("saved-1") is not None
        backend.get.assert_called_once_with(STATE_KEY_EVENTS)

    def test_initialize_without_backend_does_nothing(self):
        """StateBackend 없이 initialize() 호출 시 에러 없이 통과."""
        cal = _make_calendar()
        cal.initialize()

    def test_cancel_persists_to_backend(self):
        """이벤트 취소 시 StateBackend.set()이 호출된다."""
        backend = MagicMock()
        cal = _make_calendar(state_backend=backend)
        event = _make_event()
        cal.register(event)
        backend.set.reset_mock()
        cal.cancel(event.event_id)
        backend.set.assert_called_once()

    def test_check_drift_refreshes_when_ttl_expired(self):
        """캐시 TTL 경과 시 check_drift()가 True 반환 (refresh 수행)."""
        backend = MagicMock()
        backend.get.return_value = {}
        cal = EventCalendar(
            state_backend=backend,
            settings=CapacityReservationSettings(),
            cache_ttl_seconds=0,
        )
        cal._last_load_time = 0.0
        assert cal.check_drift() is True


# =============================================================================
# G. Behavior — Thread Safety
# =============================================================================


class TestEventCalendarThreadSafetyBehavior:
    """멀티스레드 동시 접근 안전성 검증."""

    def test_concurrent_register_no_data_corruption(self):
        """10개 스레드가 동시에 register해도 데이터 손상 없음."""
        cal = _make_calendar()
        errors = []
        count = 10

        def worker(idx):
            try:
                event = _make_event(
                    event_id=f"thread-{idx}",
                    start_time=_future(30 + idx),
                    end_time=_future(90 + idx),
                )
                cal.register(event)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(count)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        for i in range(count):
            assert cal.get_event(f"thread-{i}") is not None


# =============================================================================
# H. Behavior — warmup_time property
# =============================================================================


class TestScheduledEventWarmupTimeBehavior:
    """warmup_time 속성 동작 검증."""

    def test_warmup_time_is_start_minus_warmup_minutes(self):
        """warmup_time == start_time - warmup_minutes."""
        start = _future(60)
        event = _make_event(start_time=start, warmup_minutes=10)
        expected = start - timedelta(minutes=10)
        assert event.warmup_time == expected

    def test_timezone_naive_auto_converts_to_utc(self):
        """timezone-naive datetime은 UTC로 자동 변환."""
        naive = datetime(2026, 6, 15, 12, 0, 0)
        event = ScheduledEvent(
            name="naive-test",
            start_time=naive,
            end_time=naive + timedelta(hours=1),
        )
        assert event.start_time.tzinfo == UTC
        assert event.end_time.tzinfo == UTC
