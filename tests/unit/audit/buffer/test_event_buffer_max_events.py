"""
RequestAuditBuffer MAX_EVENTS_PER_REQUEST 제한 테스트.

단일 요청에서 무제한 이벤트 누적으로 인한 메모리 폭발을 방지하기 위해
RequestAuditBuffer에 최대 이벤트 수 제한 기능을 테스트합니다.

RingBuffer 통합 후 변경된 동작:
- DROP_OLDEST 전략: 새 이벤트 우선 (오래된 이벤트 제거)
- add_event()는 항상 True 반환
- add()는 항상 이벤트 반환
- 기본 capacity는 RingBufferSettings에서 로드 (10,000)

테스트 항목:
- 생성자 인자로 max_events 설정
- DROP_OLDEST 동작 확인
- 버퍼 통계 (stats)
- to_dict()에 truncation 정보 포함
"""

import pytest

from baldur.audit.event_buffer import (
    AuditEvent,
    AuditEventType,
    RequestAuditBuffer,
)


class TestRequestAuditBufferMaxEvents:
    """RequestAuditBuffer 최대 이벤트 수 제한 테스트."""

    def test_default_max_events_is_from_settings(self):
        """기본 max_events는 RingBufferSettings.capacity에서 로드."""
        from baldur.settings.ring_buffer import get_ring_buffer_settings

        settings = get_ring_buffer_settings()
        buffer = RequestAuditBuffer()

        assert buffer.max_events == settings.capacity

    def test_max_events_custom_value(self):
        """생성자 인자로 max_events 커스텀 설정."""
        buffer = RequestAuditBuffer(max_events=50)
        assert buffer.max_events == 50

    def test_add_event_under_limit(self):
        """한도 미만일 때 이벤트 정상 추가."""
        buffer = RequestAuditBuffer(max_events=5)

        for i in range(5):
            event = AuditEvent(
                event_type=AuditEventType.GENERIC,
                source=f"test_{i}",
            )
            result = buffer.add_event(event)
            assert result is True

        assert buffer.event_count() == 5
        assert buffer.truncated_count == 0
        assert buffer.is_truncated is False

    def test_add_event_over_limit_drops_oldest(self):
        """한도 초과 시 DROP_OLDEST 전략으로 오래된 이벤트 제거."""
        buffer = RequestAuditBuffer(max_events=3)

        # 5개 이벤트 추가 (2개 DROP)
        for i in range(5):
            event = AuditEvent(event_type=AuditEventType.GENERIC, source=f"test_{i}")
            result = buffer.add_event(event)
            assert result is True  # DROP_OLDEST는 항상 True

        # 최신 3개만 남음
        assert buffer.event_count() == 3
        assert buffer.truncated_count == 2
        assert buffer.is_truncated is True

        # 오래된 것(0, 1)이 제거됨, 최신(2, 3, 4)만 남음
        assert buffer.events[0].source == "test_2"
        assert buffer.events[1].source == "test_3"
        assert buffer.events[2].source == "test_4"

    def test_add_method_always_returns_event(self):
        """add() 메서드는 DROP_OLDEST 전략으로 항상 이벤트 반환."""
        buffer = RequestAuditBuffer(max_events=2)

        # 3개 추가 (1개 DROP)
        event1 = buffer.add(event_type=AuditEventType.ERROR_DETECTED, source="test1")
        event2 = buffer.add(event_type=AuditEventType.ERROR_DETECTED, source="test2")
        event3 = buffer.add(event_type=AuditEventType.ERROR_DETECTED, source="test3")

        # 모든 add()가 이벤트 반환
        assert event1 is not None
        assert event2 is not None
        assert event3 is not None

        # 최신 2개만 버퍼에 남음
        assert buffer.event_count() == 2
        assert buffer.truncated_count == 1

    def test_truncated_count_increments(self):
        """한도 초과 시 truncated_count (= dropped count) 증가."""
        buffer = RequestAuditBuffer(max_events=2)

        buffer.add(event_type=AuditEventType.GENERIC, source="test1")
        buffer.add(event_type=AuditEventType.GENERIC, source="test2")

        # 5개 더 추가 시도 (5개 DROP)
        for i in range(5):
            buffer.add(event_type=AuditEventType.GENERIC, source=f"overflow_{i}")

        assert buffer.event_count() == 2
        assert buffer.truncated_count == 5  # DROP된 수

    def test_to_dict_includes_truncation_info(self):
        """to_dict()에 truncation 정보 포함."""
        buffer = RequestAuditBuffer(max_events=3)

        for i in range(5):
            buffer.add(event_type=AuditEventType.GENERIC, source=f"test_{i}")

        result = buffer.to_dict()

        assert result["event_count"] == 3
        assert result["truncated"] is True
        assert result["truncated_count"] == 2
        assert result["max_events"] == 3
        assert "buffer_stats" in result

    def test_to_dict_no_truncation_info_when_not_truncated(self):
        """truncation 없으면 to_dict()에 truncated 필드 없음."""
        buffer = RequestAuditBuffer(max_events=10)

        buffer.add(event_type=AuditEventType.GENERIC, source="test")

        result = buffer.to_dict()

        assert result["event_count"] == 1
        assert "truncated" not in result
        assert "truncated_count" not in result
        assert "max_events" not in result

    def test_clear_resets_truncated_count(self):
        """clear() 호출 시 truncated_count 및 통계 초기화."""
        buffer = RequestAuditBuffer(max_events=2)

        buffer.add(event_type=AuditEventType.GENERIC, source="test1")
        buffer.add(event_type=AuditEventType.GENERIC, source="test2")
        buffer.add(event_type=AuditEventType.GENERIC, source="overflow")

        assert buffer.truncated_count == 1

        buffer.clear()

        assert buffer.event_count() == 0
        assert buffer.truncated_count == 0
        assert buffer.is_truncated is False
        assert buffer.stats["total_enqueued"] == 0

    def test_stats_property(self):
        """버퍼 통계 확인."""
        buffer = RequestAuditBuffer(max_events=5)

        for i in range(10):
            buffer.add(event_type=AuditEventType.GENERIC, source=f"test_{i}")

        stats = buffer.stats

        assert stats["capacity"] == 5
        assert stats["size"] == 5
        assert stats["total_enqueued"] == 10
        assert stats["total_dropped"] == 5
        assert stats["drop_rate"] == pytest.approx(0.5, rel=0.01)


class TestRequestAuditBufferProperties:
    """RequestAuditBuffer 속성 접근자 테스트."""

    def test_max_events_property(self):
        """max_events 속성 접근."""
        buffer = RequestAuditBuffer(max_events=42)
        assert buffer.max_events == 42

    def test_truncated_count_property(self):
        """truncated_count 속성 접근."""
        buffer = RequestAuditBuffer(max_events=1)
        buffer.add(event_type=AuditEventType.GENERIC, source="test1")
        buffer.add(event_type=AuditEventType.GENERIC, source="test2")

        assert buffer.truncated_count == 1

    def test_is_truncated_property(self):
        """is_truncated 속성 접근."""
        buffer = RequestAuditBuffer(max_events=1)

        assert buffer.is_truncated is False

        buffer.add(event_type=AuditEventType.GENERIC, source="test1")
        buffer.add(event_type=AuditEventType.GENERIC, source="test2")

        assert buffer.is_truncated is True
