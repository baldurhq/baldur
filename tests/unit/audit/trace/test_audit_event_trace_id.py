"""
AuditEvent TraceID 자동 포함 테스트.

테스트 대상:
1. AuditEvent.trace_id 필드 존재
2. __post_init__()에서 trace_id 자동 설정
3. to_dict()에서 trace_id 포함
"""

from unittest.mock import patch

from baldur.audit.event_buffer import AuditEvent, AuditEventType


class TestAuditEventTraceID:
    """AuditEvent TraceID 자동 포함 테스트."""

    def test_audit_event_has_trace_id_field(self):
        """AuditEvent에 trace_id 필드 존재."""
        event = AuditEvent(
            event_type=AuditEventType.DLQ_STORE,
            source="test",
        )

        assert hasattr(event, "trace_id")

    def test_trace_id_auto_set_from_trace_module(self):
        """trace 모듈에서 trace_id 자동 설정."""
        with patch(
            "baldur.audit.trace.get_trace_id",
            return_value="req-test-12345678",
        ):
            event = AuditEvent(
                event_type=AuditEventType.CONFIG_CHANGE,
                source="test",
            )

        assert event.trace_id == "req-test-12345678"

    def test_trace_id_can_be_explicitly_set(self):
        """trace_id 명시적 설정 가능."""
        event = AuditEvent(
            event_type=AuditEventType.CB_STATE_CHANGE,
            source="test",
            trace_id="custom-trace-id",
        )

        assert event.trace_id == "custom-trace-id"

    def test_explicit_trace_id_not_overwritten(self):
        """명시적으로 설정된 trace_id는 덮어쓰지 않음."""
        with patch(
            "baldur.audit.trace.get_trace_id",
            return_value="req-auto-trace",
        ):
            event = AuditEvent(
                event_type=AuditEventType.DLQ_STORE,
                source="test",
                trace_id="explicit-trace-id",
            )

        # 명시적 설정 유지
        assert event.trace_id == "explicit-trace-id"

    def test_to_dict_includes_trace_id(self):
        """to_dict()에 trace_id 포함."""
        event = AuditEvent(
            event_type=AuditEventType.ERROR_DETECTED,
            source="test",
            trace_id="req-dict-test",
        )

        event_dict = event.to_dict()

        assert "trace_id" in event_dict
        assert event_dict["trace_id"] == "req-dict-test"

    def test_trace_id_none_when_trace_module_unavailable(self):
        """trace 모듈 미사용 환경에서 trace_id는 None."""
        with patch(
            "baldur.audit.event_buffer.AuditEvent.__post_init__",
            lambda self: None,
        ):
            event = AuditEvent(
                event_type=AuditEventType.GENERIC,
                source="test",
            )

        # trace_id 기본값 None
        assert event.trace_id is None

    def test_to_dict_trace_id_serialization(self):
        """trace_id가 None일 때도 to_dict() 정상 동작."""
        event = AuditEvent(
            event_type=AuditEventType.RATE_LIMITED,
            source="test",
        )
        # post_init에서 설정될 수 있지만, 없어도 None으로 직렬화
        event.trace_id = None

        event_dict = event.to_dict()

        assert "trace_id" in event_dict
        assert event_dict["trace_id"] is None
