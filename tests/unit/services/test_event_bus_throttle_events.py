"""
Throttle EventType 및 EventBus 채널 매핑 테스트.

Throttle 이벤트 4종:
- THROTTLE_LIMIT_CHANGED: limit 변경 시 발행
- THROTTLE_SLA_WARNING: SLA Warning 임계값 도달
- THROTTLE_SLA_CRITICAL: SLA Critical 임계값 도달
- THROTTLE_LIMIT_RECOVERED: limit 정상 범위 회복
"""

from baldur.services.event_bus import BaldurEvent, EventPriority, EventType


class TestThrottleEventTypes:
    """Throttle 이벤트 타입 정의 테스트."""

    def test_throttle_limit_changed_event_type_exists(self):
        """THROTTLE_LIMIT_CHANGED 이벤트 타입 존재 확인."""
        assert hasattr(EventType, "THROTTLE_LIMIT_CHANGED")
        assert EventType.THROTTLE_LIMIT_CHANGED.value == "throttle_limit_changed"

    def test_throttle_sla_warning_event_type_exists(self):
        """THROTTLE_SLA_WARNING 이벤트 타입 존재 확인."""
        assert hasattr(EventType, "THROTTLE_SLA_WARNING")
        assert EventType.THROTTLE_SLA_WARNING.value == "throttle_sla_warning"

    def test_throttle_sla_critical_event_type_exists(self):
        """THROTTLE_SLA_CRITICAL 이벤트 타입 존재 확인."""
        assert hasattr(EventType, "THROTTLE_SLA_CRITICAL")
        assert EventType.THROTTLE_SLA_CRITICAL.value == "throttle_sla_critical"

    def test_throttle_limit_recovered_event_type_exists(self):
        """THROTTLE_LIMIT_RECOVERED 이벤트 타입 존재 확인."""
        assert hasattr(EventType, "THROTTLE_LIMIT_RECOVERED")
        assert EventType.THROTTLE_LIMIT_RECOVERED.value == "throttle_limit_recovered"


class TestThrottleEventChannel:
    """Throttle EventChannel 정의 테스트."""

    def test_throttle_channel_exists(self):
        """THROTTLE 채널 존재 확인."""
        from baldur.services.event_bus.redis_bus import EventChannel

        assert hasattr(EventChannel, "THROTTLE")
        assert EventChannel.THROTTLE.value == "throttle"


class TestThrottleEventChannelMapping:
    """Throttle 이벤트 채널 매핑 테스트."""

    def test_throttle_channel_in_baldur_event_channels(self):
        """BALDUR_EVENT_CHANNELS에 throttle 채널 포함 확인."""
        from baldur.services.event_bus.redis_bus import (
            BALDUR_EVENT_CHANNELS,
            EventChannel,
        )

        assert EventChannel.THROTTLE.value in BALDUR_EVENT_CHANNELS
        assert (
            BALDUR_EVENT_CHANNELS[EventChannel.THROTTLE.value]
            == "baldur:events:throttle"
        )

    def test_throttle_limit_changed_mapped_to_throttle_channel(self):
        """THROTTLE_LIMIT_CHANGED는 THROTTLE 채널로 매핑."""
        from baldur.services.event_bus.redis_bus import (
            EVENT_TYPE_TO_CHANNEL,
            EventChannel,
        )

        assert (
            EVENT_TYPE_TO_CHANNEL[EventType.THROTTLE_LIMIT_CHANGED]
            == EventChannel.THROTTLE
        )

    def test_throttle_sla_warning_mapped_to_throttle_channel(self):
        """THROTTLE_SLA_WARNING은 THROTTLE 채널로 매핑."""
        from baldur.services.event_bus.redis_bus import (
            EVENT_TYPE_TO_CHANNEL,
            EventChannel,
        )

        assert (
            EVENT_TYPE_TO_CHANNEL[EventType.THROTTLE_SLA_WARNING]
            == EventChannel.THROTTLE
        )

    def test_throttle_sla_critical_mapped_to_global_channel(self):
        """THROTTLE_SLA_CRITICAL은 GLOBAL 채널로 매핑 (전체 클러스터 알림)."""
        from baldur.services.event_bus.redis_bus import (
            EVENT_TYPE_TO_CHANNEL,
            EventChannel,
        )

        assert (
            EVENT_TYPE_TO_CHANNEL[EventType.THROTTLE_SLA_CRITICAL]
            == EventChannel.GLOBAL
        )

    def test_throttle_limit_recovered_mapped_to_throttle_channel(self):
        """THROTTLE_LIMIT_RECOVERED는 THROTTLE 채널로 매핑."""
        from baldur.services.event_bus.redis_bus import (
            EVENT_TYPE_TO_CHANNEL,
            EventChannel,
        )

        assert (
            EVENT_TYPE_TO_CHANNEL[EventType.THROTTLE_LIMIT_RECOVERED]
            == EventChannel.THROTTLE
        )


class TestThrottleEventCreation:
    """Throttle 이벤트 생성 테스트."""

    def test_create_throttle_limit_changed_event(self):
        """THROTTLE_LIMIT_CHANGED 이벤트 생성 확인."""
        event = BaldurEvent(
            event_type=EventType.THROTTLE_LIMIT_CHANGED,
            data={
                "previous_limit": 100,
                "new_limit": 50,
                "reason": "rtt_increase",
                "gradient": -0.5,
                "rtt_ms": 250.0,
            },
            source="throttle",
        )

        assert event.event_type == EventType.THROTTLE_LIMIT_CHANGED
        assert event.data["previous_limit"] == 100
        assert event.data["new_limit"] == 50
        assert event.source == "throttle"

    def test_create_throttle_sla_warning_event(self):
        """THROTTLE_SLA_WARNING 이벤트 생성 확인."""
        event = BaldurEvent(
            event_type=EventType.THROTTLE_SLA_WARNING,
            data={
                "current_rtt_ms": 180.0,
                "threshold_ms": 150.0,
                "current_limit": 75,
                "gradient": -0.3,
            },
            source="throttle",
            priority=EventPriority.HIGH,
        )

        assert event.event_type == EventType.THROTTLE_SLA_WARNING
        assert event.data["current_rtt_ms"] == 180.0
        assert event.priority == EventPriority.HIGH

    def test_create_throttle_sla_critical_event(self):
        """THROTTLE_SLA_CRITICAL 이벤트 생성 확인."""
        event = BaldurEvent(
            event_type=EventType.THROTTLE_SLA_CRITICAL,
            data={
                "current_rtt_ms": 350.0,
                "threshold_ms": 300.0,
                "current_limit": 30,
                "reduction_percent": 0.7,
            },
            source="throttle",
            priority=EventPriority.CRITICAL,
        )

        assert event.event_type == EventType.THROTTLE_SLA_CRITICAL
        assert event.data["reduction_percent"] == 0.7
        assert event.priority == EventPriority.CRITICAL

    def test_create_throttle_limit_recovered_event(self):
        """THROTTLE_LIMIT_RECOVERED 이벤트 생성 확인."""
        event = BaldurEvent(
            event_type=EventType.THROTTLE_LIMIT_RECOVERED,
            data={
                "previous_limit": 30,
                "new_limit": 100,
                "recovery_duration_ms": 5000,
            },
            source="throttle",
        )

        assert event.event_type == EventType.THROTTLE_LIMIT_RECOVERED
        assert event.data["recovery_duration_ms"] == 5000


class TestThrottleEventSerialization:
    """Throttle 이벤트 직렬화 테스트."""

    def test_throttle_event_to_dict(self):
        """Throttle 이벤트 to_dict() 테스트."""
        event = BaldurEvent(
            event_type=EventType.THROTTLE_LIMIT_CHANGED,
            data={
                "previous_limit": 100,
                "new_limit": 50,
                "reason": "rtt_increase",
            },
            source="throttle",
        )

        event_dict = event.to_dict()

        assert event_dict["event_type"] == "throttle_limit_changed"
        assert event_dict["source"] == "throttle"
        assert event_dict["data"]["previous_limit"] == 100


class TestAllChannelsCovered:
    """모든 EventChannel이 BALDUR_EVENT_CHANNELS에 포함되는지 확인."""

    def test_all_channels_have_redis_key(self):
        """모든 EventChannel이 Redis 키 매핑을 갖는지 확인."""
        from baldur.services.event_bus.redis_bus import (
            BALDUR_EVENT_CHANNELS,
            EventChannel,
        )

        for channel in EventChannel:
            assert channel.value in BALDUR_EVENT_CHANNELS, (
                f"Channel {channel.value} not in BALDUR_EVENT_CHANNELS"
            )
