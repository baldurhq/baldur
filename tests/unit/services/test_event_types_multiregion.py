"""
EventType Multi-Region 이벤트 계약 테스트.

테스트 대상 (237):
- EventType.REGION_INSTANCE_STOPPING
- EventType.REGION_HEARTBEAT_EXPIRED
- EventType.REGION_PRIMARY_CHANGED
"""

from __future__ import annotations

from baldur.services.event_bus.bus import EventType


class TestMultiRegionEventTypeContract:
    """Multi-Region EventType 계약값 검증."""

    def test_region_instance_stopping(self) -> None:
        """REGION_INSTANCE_STOPPING 값: 'region_instance_stopping'."""
        assert EventType.REGION_INSTANCE_STOPPING.value == "region_instance_stopping"

    def test_region_heartbeat_expired(self) -> None:
        """REGION_HEARTBEAT_EXPIRED 값: 'region_heartbeat_expired'."""
        assert EventType.REGION_HEARTBEAT_EXPIRED.value == "region_heartbeat_expired"

    def test_region_primary_changed(self) -> None:
        """REGION_PRIMARY_CHANGED 값: 'region_primary_changed'."""
        assert EventType.REGION_PRIMARY_CHANGED.value == "region_primary_changed"

    def test_all_multiregion_events_are_unique(self) -> None:
        """3개 멀티리전 이벤트 값이 모두 고유하다."""
        values = [
            EventType.REGION_INSTANCE_STOPPING.value,
            EventType.REGION_HEARTBEAT_EXPIRED.value,
            EventType.REGION_PRIMARY_CHANGED.value,
        ]
        assert len(values) == len(set(values))

    def test_multiregion_events_are_str_enum(self) -> None:
        """Multi-Region 이벤트도 str Enum이다."""
        assert isinstance(EventType.REGION_INSTANCE_STOPPING, str)
        assert isinstance(EventType.REGION_HEARTBEAT_EXPIRED, str)
        assert isinstance(EventType.REGION_PRIMARY_CHANGED, str)
