"""
Unit tests for JournalSubscriber and _JournalCircuitBreaker.

검증 항목:
- JOURNALED_EVENT_TYPES 계약값 (7개 이벤트 타입)
- _JournalCircuitBreaker 상태 전이
- JournalSubscriber.register() 구독 등록
- _handle_event() 정상/에러 동작
- _build_entry() 변환 로직
- 에러 격리 원칙 (저널링 실패가 메인 로직 중단시키지 않음)

테스트 대상: baldur.services.event_journal.subscriber
"""

import time
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from baldur.interfaces.event_journal import EventJournalRepository, JournalEntry
from baldur.services.event_bus.bus import BaldurEvent, EventType
from baldur.services.event_journal.subscriber import (
    JOURNALED_EVENT_TYPES,
    JournalSubscriber,
    _JournalCircuitBreaker,
)


def _make_event(
    event_type: EventType = EventType.CIRCUIT_BREAKER_OPENED,
    source: str = "test",
    data: dict | None = None,
    timestamp: datetime | None = None,
) -> BaldurEvent:
    """테스트용 BaldurEvent 생성 헬퍼."""
    return BaldurEvent(
        event_type=event_type,
        source=source,
        data=data or {"service_name": "svc-a", "region": "us-east-1"},
        timestamp=timestamp or datetime(2026, 3, 1, 12, 0, 0, tzinfo=UTC),
    )


class TestJournaledEventTypesContract:
    """JOURNALED_EVENT_TYPES 설계 계약값 검증."""

    def test_journaled_event_types_count_is_7(self):
        """구독 이벤트 타입은 정확히 7개."""
        assert len(JOURNALED_EVENT_TYPES) == 7

    def test_journaled_event_types_contains_circuit_breaker_events(self):
        """Circuit Breaker 관련 3개 이벤트 포함."""
        assert EventType.CIRCUIT_BREAKER_OPENED in JOURNALED_EVENT_TYPES
        assert EventType.CIRCUIT_BREAKER_CLOSED in JOURNALED_EVENT_TYPES
        assert EventType.CIRCUIT_BREAKER_HALF_OPENED in JOURNALED_EVENT_TYPES

    def test_journaled_event_types_contains_error_budget_events(self):
        """Error Budget 관련 3개 이벤트 포함."""
        assert EventType.ERROR_BUDGET_CRITICAL in JOURNALED_EVENT_TYPES
        assert EventType.ERROR_BUDGET_WARNING in JOURNALED_EVENT_TYPES
        assert EventType.ERROR_BUDGET_RECOVERED in JOURNALED_EVENT_TYPES

    def test_journaled_event_types_contains_emergency_event(self):
        """Emergency Level Changed 이벤트 포함."""
        assert EventType.EMERGENCY_LEVEL_CHANGED in JOURNALED_EVENT_TYPES

    def test_journaled_event_types_is_frozenset(self):
        """JOURNALED_EVENT_TYPES는 frozenset 타입이다."""
        assert isinstance(JOURNALED_EVENT_TYPES, frozenset)


class TestJournalCircuitBreakerContract:
    """_JournalCircuitBreaker 설계 계약값 검증."""

    def test_default_failure_threshold_is_5(self):
        """기본 실패 임계값: 5."""
        cb = _JournalCircuitBreaker()
        assert cb._threshold == 5

    def test_default_recovery_seconds_is_30(self):
        """기본 복구 대기 시간: 30초."""
        cb = _JournalCircuitBreaker()
        assert cb._recovery == 30


class TestJournalCircuitBreakerStateBehavior:
    """_JournalCircuitBreaker 상태 전이 검증."""

    def test_initially_closed(self):
        """초기 상태는 닫힘(is_open=False)."""
        cb = _JournalCircuitBreaker()
        assert cb.is_open() is False

    def test_opens_after_reaching_failure_threshold(self):
        """failure_threshold만큼 실패하면 열린다."""
        cb = _JournalCircuitBreaker(failure_threshold=3, recovery_seconds=60)
        cb.record_failure()
        cb.record_failure()
        assert cb.is_open() is False
        cb.record_failure()
        assert cb.is_open() is True

    def test_record_success_resets_failure_count(self):
        """성공 기록 시 실패 카운터가 리셋된다."""
        cb = _JournalCircuitBreaker(failure_threshold=3)
        cb.record_failure()
        cb.record_failure()
        cb.record_success()
        assert cb._failures == 0
        cb.record_failure()
        assert cb.is_open() is False

    def test_closes_after_recovery_period(self):
        """recovery_seconds 경과 후 CB가 닫힌다."""
        cb = _JournalCircuitBreaker(failure_threshold=1, recovery_seconds=10)

        # record_failure 시점의 monotonic을 제어
        base_time = 1000.0
        with patch("time.monotonic", return_value=base_time):
            cb.record_failure()

        # recovery 이전 — 아직 열림
        with patch("time.monotonic", return_value=base_time + 5):
            assert cb.is_open() is True

        # recovery 경과 후 — 닫힘
        with patch("time.monotonic", return_value=base_time + 11):
            assert cb.is_open() is False


class TestJournalSubscriberRegisterBehavior:
    """JournalSubscriber.register() 검증."""

    def test_register_subscribes_to_all_journaled_event_types(self):
        """register()는 JOURNALED_EVENT_TYPES의 모든 이벤트에 구독한다."""
        mock_repo = MagicMock(spec=EventJournalRepository)
        mock_bus = MagicMock()
        subscriber = JournalSubscriber(repository=mock_repo)

        subscriber.register(mock_bus)

        assert mock_bus.subscribe.call_count == len(JOURNALED_EVENT_TYPES)
        subscribed_types = {call[0][0] for call in mock_bus.subscribe.call_args_list}
        assert subscribed_types == set(JOURNALED_EVENT_TYPES)


class TestJournalSubscriberHandleEventBehavior:
    """_handle_event() 동작 검증."""

    def test_handle_event_appends_entry_to_repository(self):
        """정상 이벤트를 저널에 추가한다."""
        mock_repo = MagicMock(spec=EventJournalRepository)
        subscriber = JournalSubscriber(repository=mock_repo)

        event = _make_event()
        subscriber._handle_event(event)

        mock_repo.append.assert_called_once()
        appended_entry = mock_repo.append.call_args[0][0]
        assert isinstance(appended_entry, JournalEntry)
        assert appended_entry.event_type == EventType.CIRCUIT_BREAKER_OPENED.value

    def test_handle_event_does_not_propagate_repository_exception(self):
        """저장소 예외가 호출자에게 전파되지 않는다 (에러 격리)."""
        mock_repo = MagicMock(spec=EventJournalRepository)
        mock_repo.append.side_effect = ConnectionError("Redis down")
        subscriber = JournalSubscriber(repository=mock_repo)

        event = _make_event()
        subscriber._handle_event(event)  # should not raise

    def test_handle_event_records_failure_on_repository_exception(self):
        """저장소 예외 시 CB에 실패를 기록한다."""
        mock_repo = MagicMock(spec=EventJournalRepository)
        mock_repo.append.side_effect = ConnectionError("Redis down")
        subscriber = JournalSubscriber(repository=mock_repo)

        event = _make_event()
        subscriber._handle_event(event)

        assert subscriber._cb._failures == 1

    def test_handle_event_records_success_on_successful_append(self):
        """정상 append 후 CB에 성공을 기록한다."""
        mock_repo = MagicMock(spec=EventJournalRepository)
        subscriber = JournalSubscriber(repository=mock_repo)

        # Record some failures first
        subscriber._cb._failures = 3
        event = _make_event()
        subscriber._handle_event(event)

        assert subscriber._cb._failures == 0

    def test_handle_event_skips_when_circuit_breaker_open(self):
        """CB가 열려있으면 이벤트를 무시한다."""
        mock_repo = MagicMock(spec=EventJournalRepository)
        subscriber = JournalSubscriber(repository=mock_repo)
        subscriber._cb._failures = 5
        subscriber._cb._open_until = time.monotonic() + 9999

        event = _make_event()
        subscriber._handle_event(event)

        mock_repo.append.assert_not_called()

    def test_handle_event_handles_serialization_error_separately(self):
        """TypeError/ValueError는 CB 실패로 기록하지 않는다."""
        mock_repo = MagicMock(spec=EventJournalRepository)
        subscriber = JournalSubscriber(repository=mock_repo)

        # Make _build_entry raise TypeError
        with patch.object(
            subscriber, "_build_entry", side_effect=TypeError("bad data")
        ):
            event = _make_event()
            subscriber._handle_event(event)  # should not raise

        assert subscriber._cb._failures == 0  # Not counted as infra failure


class TestJournalSubscriberBuildEntryBehavior:
    """_build_entry() 변환 로직 검증."""

    def test_build_entry_maps_event_fields_correctly(self):
        """BaldurEvent를 JournalEntry로 올바르게 변환한다."""
        mock_repo = MagicMock(spec=EventJournalRepository)
        subscriber = JournalSubscriber(repository=mock_repo)

        ts = datetime(2026, 3, 1, 12, 0, 0, tzinfo=UTC)
        event = _make_event(
            event_type=EventType.ERROR_BUDGET_CRITICAL,
            source="error-budget-service",
            data={
                "service_name": "payment",
                "region": "eu-west-1",
                "tier_id": "gold",
                "remaining": 0.05,
            },
            timestamp=ts,
        )

        entry = subscriber._build_entry(event)

        assert entry.sequence == 0
        assert entry.event_type == "error_budget_critical"
        assert entry.source == "error-budget-service"
        assert entry.timestamp == ts
        assert entry.service_name == "payment"
        assert entry.region == "eu-west-1"
        assert entry.tier_id == "gold"

    def test_build_entry_uses_defensive_serialization(self):
        """_build_entry()는 방어적 직렬화 (json.dumps + default=str)를 적용한다."""
        mock_repo = MagicMock(spec=EventJournalRepository)
        subscriber = JournalSubscriber(repository=mock_repo)

        non_serializable_ts = datetime(2026, 3, 1, tzinfo=UTC)
        event = _make_event(
            data={
                "service_name": "svc",
                "timestamp_val": non_serializable_ts,
            }
        )

        entry = subscriber._build_entry(event)

        # datetime should be converted to string by default=str
        assert isinstance(entry.context["timestamp_val"], str)

    def test_build_entry_defaults_service_name_to_empty_string(self):
        """data에 service_name이 없으면 빈 문자열을 사용한다."""
        mock_repo = MagicMock(spec=EventJournalRepository)
        subscriber = JournalSubscriber(repository=mock_repo)

        event = _make_event(data={"some_key": "value"})
        entry = subscriber._build_entry(event)

        assert entry.service_name == ""

    def test_build_entry_defaults_region_to_empty_string(self):
        """data에 region이 없으면 빈 문자열을 사용한다."""
        mock_repo = MagicMock(spec=EventJournalRepository)
        subscriber = JournalSubscriber(repository=mock_repo)

        event = _make_event(data={"service_name": "svc"})
        entry = subscriber._build_entry(event)

        assert entry.region == ""

    def test_build_entry_defaults_tier_id_to_empty_string(self):
        """data에 tier_id가 없으면 빈 문자열을 사용한다."""
        mock_repo = MagicMock(spec=EventJournalRepository)
        subscriber = JournalSubscriber(repository=mock_repo)

        event = _make_event(data={"service_name": "svc"})
        entry = subscriber._build_entry(event)

        assert entry.tier_id == ""
