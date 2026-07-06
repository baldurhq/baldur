"""
DLQ + Throttle EventType 및 EventChannel 매핑 단위 테스트.

테스트 대상:
- baldur.services.event_bus.EventType (4개 신규 이벤트)
- baldur.services.event_bus.redis_bus.EVENT_TYPE_TO_CHANNEL 매핑

테스트 시나리오:
1. 4개 신규 EventType 존재 확인
2. EventType 값 형식 검증 (snake_case)
3. EVENT_TYPE_TO_CHANNEL에 4개 이벤트 모두 매핑 확인
4. 매핑 채널이 THROTTLE인지 확인
"""

from baldur.services.event_bus import EventType


class TestThrottleDLQEventTypes:
    """Throttle DLQ 관련 EventType 존재 확인 테스트."""

    def test_throttle_rejection_stored_exists(self):
        """THROTTLE_REJECTION_STORED EventType이 존재한다."""
        assert hasattr(EventType, "THROTTLE_REJECTION_STORED")

    def test_throttle_rejection_replay_started_exists(self):
        """THROTTLE_REJECTION_REPLAY_STARTED EventType이 존재한다."""
        assert hasattr(EventType, "THROTTLE_REJECTION_REPLAY_STARTED")

    def test_throttle_rejection_replay_completed_exists(self):
        """THROTTLE_REJECTION_REPLAY_COMPLETED EventType이 존재한다."""
        assert hasattr(EventType, "THROTTLE_REJECTION_REPLAY_COMPLETED")

    def test_throttle_rejection_replay_failed_exists(self):
        """THROTTLE_REJECTION_REPLAY_FAILED EventType이 존재한다."""
        assert hasattr(EventType, "THROTTLE_REJECTION_REPLAY_FAILED")

    def test_event_type_values_are_snake_case(self):
        """EventType 값이 snake_case 형식이다."""
        assert EventType.THROTTLE_REJECTION_STORED.value == "throttle_rejection_stored"
        assert (
            EventType.THROTTLE_REJECTION_REPLAY_STARTED.value
            == "throttle_rejection_replay_started"
        )
        assert (
            EventType.THROTTLE_REJECTION_REPLAY_COMPLETED.value
            == "throttle_rejection_replay_completed"
        )
        assert (
            EventType.THROTTLE_REJECTION_REPLAY_FAILED.value
            == "throttle_rejection_replay_failed"
        )


class TestThrottleDLQEventChannelMapping:
    """EVENT_TYPE_TO_CHANNEL 매핑 테스트."""

    def test_all_dlq_events_mapped_to_channel(self):
        """4개 DLQ 이벤트 모두 EVENT_TYPE_TO_CHANNEL에 매핑되어 있다."""
        from baldur.services.event_bus.redis_bus import EVENT_TYPE_TO_CHANNEL

        dlq_event_types = [
            EventType.THROTTLE_REJECTION_STORED,
            EventType.THROTTLE_REJECTION_REPLAY_STARTED,
            EventType.THROTTLE_REJECTION_REPLAY_COMPLETED,
            EventType.THROTTLE_REJECTION_REPLAY_FAILED,
        ]

        for event_type in dlq_event_types:
            assert event_type in EVENT_TYPE_TO_CHANNEL, (
                f"{event_type.name} not mapped in EVENT_TYPE_TO_CHANNEL"
            )

    def test_dlq_events_mapped_to_throttle_channel(self):
        """DLQ 이벤트들이 THROTTLE 채널에 매핑된다."""
        from baldur.services.event_bus.redis_bus import (
            EVENT_TYPE_TO_CHANNEL,
            EventChannel,
        )

        dlq_event_types = [
            EventType.THROTTLE_REJECTION_STORED,
            EventType.THROTTLE_REJECTION_REPLAY_STARTED,
            EventType.THROTTLE_REJECTION_REPLAY_COMPLETED,
            EventType.THROTTLE_REJECTION_REPLAY_FAILED,
        ]

        for event_type in dlq_event_types:
            assert EVENT_TYPE_TO_CHANNEL[event_type] == EventChannel.THROTTLE
