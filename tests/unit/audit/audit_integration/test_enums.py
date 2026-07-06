"""
EventSeverity and AuditEventType Enum Tests.

Enum 테스트.
Uses lazy imports to avoid Prometheus registry conflicts.
"""


class TestEventSeverity:
    """EventSeverity enum 테스트."""

    def test_severity_values(self):
        """심각도 값 검증."""
        from baldur.audit.audit_integration import EventSeverity

        assert EventSeverity.DEBUG.value == 0
        assert EventSeverity.INFO.value == 1
        assert EventSeverity.WARNING.value == 2
        assert EventSeverity.CRITICAL.value == 3

    def test_severity_comparison(self):
        """심각도 비교."""
        from baldur.audit.audit_integration import EventSeverity

        assert EventSeverity.CRITICAL.value > EventSeverity.WARNING.value
        assert EventSeverity.WARNING.value > EventSeverity.INFO.value


class TestAuditObserverEventType:
    """AuditObserverEventType enum 테스트."""

    def test_event_types_exist(self):
        """필수 이벤트 유형 존재 확인."""
        from baldur.audit.audit_integration import AuditObserverEventType

        assert AuditObserverEventType.RECORD_SUCCESS
        assert AuditObserverEventType.RECORD_FAILED
        assert AuditObserverEventType.CIRCUIT_OPENED
        assert AuditObserverEventType.CIRCUIT_CLOSED
        assert AuditObserverEventType.FALLBACK_ACTIVATED
        assert AuditObserverEventType.SYSLOG_ACTIVATED
        assert AuditObserverEventType.PRIMARY_RECOVERED
        assert AuditObserverEventType.DEGRADED_MODE_ENTERED
