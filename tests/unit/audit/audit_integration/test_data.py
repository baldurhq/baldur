"""
AuditEventData Dataclass Tests.

AuditEventData 데이터클래스 테스트.
Uses lazy imports to avoid Prometheus registry conflicts.
"""

from datetime import datetime


class TestAuditEventData:
    """AuditEventData dataclass 테스트."""

    def test_default_timestamp(self):
        """기본 타임스탬프 생성."""
        from baldur.audit.audit_integration import (
            AuditEventData,
            AuditObserverEventType,
        )

        event = AuditEventData(event_type=AuditObserverEventType.CIRCUIT_OPENED)

        assert event.timestamp is not None
        assert isinstance(event.timestamp, datetime)

    def test_custom_details(self):
        """커스텀 details."""
        from baldur.audit.audit_integration import (
            AuditEventData,
            AuditObserverEventType,
        )

        event = AuditEventData(
            event_type=AuditObserverEventType.FALLBACK_ACTIVATED,
            details={"fallback_type": "file", "reason": "primary_failed"},
        )

        assert event.details["fallback_type"] == "file"
        assert event.details["reason"] == "primary_failed"
