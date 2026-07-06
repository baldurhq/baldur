"""
Test Event Buffer - 순수 Python 테스트 (Django 불필요)

56_AUDIT_MIDDLEWARE_DESIGN.md 구현 테스트:
- AuditEventType: 이벤트 유형 enum
- AuditEvent: 이벤트 데이터 클래스
- RequestAuditBuffer: 이벤트 버퍼

Test Categories:
    A. AuditEventType: existence checks, value checks
    B. AuditEventType Snapshot (349 §4): cross-service Enum contract protection
    C. AuditEvent: dataclass creation, serialization
    D. RequestAuditBuffer: add, query, lifecycle
    E. add_audit_event: convenience function
    F. Hybrid audit helpers: no-request fallback
    G. API exception event types
    H. has_event_from_source: source filtering

Django 관련 테스트는 tests/baldur/unit/test_audit_middleware.py에 있습니다.

Author: Baldur Team
"""

from datetime import datetime
from unittest.mock import MagicMock

import pytest

# =============================================================================
# Test AuditEventType
# =============================================================================


class TestAuditEventType:
    """AuditEventType enum 테스트."""

    def test_event_types_exist(self):
        """모든 필수 이벤트 타입이 존재하는지 확인."""
        from baldur.audit.event_buffer import AuditEventType

        required_types = [
            "DLQ_STORE",
            "DLQ_REPLAY",
            "CB_STATE_CHANGE",
            "CB_REJECTION",
            "GOVERNANCE_BLOCKED",
            "RATE_LIMITED",
            "POOL_CB_REJECTION",
            "ERROR_DETECTED",
            "CONFIG_CHANGE",
        ]

        for type_name in required_types:
            assert hasattr(AuditEventType, type_name), f"{type_name} should exist"

    def test_event_type_values(self):
        """이벤트 타입 값이 올바른지 확인."""
        from baldur.audit.event_buffer import AuditEventType

        assert AuditEventType.DLQ_STORE.value == "dlq_store"
        assert AuditEventType.CB_STATE_CHANGE.value == "circuit_breaker_state_change"
        assert (
            AuditEventType.POOL_CB_REJECTION.value == "pool_circuit_breaker_rejection"
        )


# =============================================================================
# B. AuditEventType Snapshot — Cross-service Enum Contract Protection (349 §4)
# =============================================================================


class TestAuditEventTypeSnapshotContract:
    """AuditEventType value snapshot for cross-service compatibility (doc §349 §4).

    AuditEventType values are serialized to JSON for Kafka/WAL transport and
    deserialized by downstream consumers (Go, Java report workers, analytics
    pipelines). These string values form an API contract — any unintended
    addition, removal, or rename breaks downstream deserialization.

    If this test fails, verify that:
    1. The change is intentional (not a typo or accidental rename)
    2. Downstream consumers have been updated to handle new/changed values
    3. Update the snapshot below to match the new contract
    """

    # Snapshot of all AuditEventType member name→value pairs.
    # Sorted alphabetically for deterministic comparison.
    EXPECTED_MEMBERS = {
        "API_AUTH_ERROR": "api_auth_error",
        "API_EXCEPTION": "api_exception",
        "API_NOT_FOUND": "api_not_found",
        "API_THROTTLED": "api_throttled",
        "API_VALIDATION_ERROR": "api_validation_error",
        "BLAST_RADIUS_ISOLATION": "blast_radius_isolation",
        "BLAST_RADIUS_VIOLATION": "blast_radius_violation",
        "CB_RECOVERY": "circuit_breaker_recovery",
        "CB_REJECTION": "circuit_breaker_rejection",
        "CB_STATE_CHANGE": "circuit_breaker_state_change",
        "CHAOS_EXPERIMENT_COMPLETED": "chaos_experiment_completed",
        "CHAOS_EXPERIMENT_STARTED": "chaos_experiment_started",
        "CHAOS_INJECTION_APPLIED": "chaos_injection_applied",
        "CHAOS_ROLLBACK_TRIGGERED": "chaos_rollback_triggered",
        "COMPLIANCE_CHECK_EXEMPTED": "compliance_check_exempted",
        "COMPLIANCE_CHECK_PASSED": "compliance_check_passed",
        "COMPLIANCE_VIOLATION": "compliance_violation",
        "CONFIG_CHANGE": "config_change",
        "CORRUPTION_BLOCKED": "corruption_blocked",
        "CORRUPTION_DETECTED": "corruption_detected",
        "DATA_ACCESS": "data_access",
        "DLQ_ESCALATE": "dlq_escalate",
        "DLQ_FORCE_REDRIVE": "dlq_force_redrive",
        "DLQ_REPLAY": "dlq_replay",
        "DLQ_STORE": "dlq_store",
        "EMERGENCY_MODE_ACTIVATED": "emergency_mode_activated",
        "EMERGENCY_MODE_DEACTIVATED": "emergency_mode_deactivated",
        "ERROR_BUDGET_BLOCKED": "error_budget_blocked",
        "ERROR_BUDGET_DEPLETED": "error_budget_depleted",
        "ERROR_BUDGET_RECOVERED": "error_budget_recovered",
        "ERROR_BUDGET_WARNING": "error_budget_warning",
        "ERROR_DETECTED": "error_detected",
        "FAILSAFE_PERIOD_ENDED": "failsafe_period_ended",
        "FAILSAFE_PERIOD_STARTED": "failsafe_period_started",
        "FINOPS_BUDGET_EXCEEDED": "finops_budget_exceeded",
        "FINOPS_THRESHOLD_EXCEEDED": "finops_threshold_exceeded",
        "FORENSIC_ANOMALY_DETECTED": "forensic_anomaly_detected",
        "FORENSIC_CAPTURE_COMPLETED": "forensic_capture_completed",
        "FORENSIC_CAPTURE_STARTED": "forensic_capture_started",
        "GENERIC": "generic",
        "GOVERNANCE_BLOCKED": "governance_blocked",
        "GOVERNANCE_KILL_SWITCH": "governance_kill_switch",
        "MANUAL_OVERRIDE": "manual_override",
        "PENDING_RECONCILIATION_FREEZE": "pending_reconciliation_freeze",
        "POOL_CB_REJECTION": "pool_circuit_breaker_rejection",
        "POOL_CB_STATE_CHANGE": "pool_circuit_breaker_state_change",
        "RATE_LIMITED": "rate_limited",
        "RECONCILIATION_ACCURACY_VERIFIED": "reconciliation_accuracy_verified",
        "RECONCILIATION_APPROVED": "reconciliation_approved",
        "RECONCILIATION_REJECTED": "reconciliation_rejected",
        "RECOVERY_CHAIN_COMPLETED": "recovery_chain_completed",
        "RECOVERY_CHAIN_STARTED": "recovery_chain_started",
        "RECOVERY_EVENT": "recovery_event",
        "REGION_ISOLATED": "region_isolated",
        "REGION_RESTORED": "region_restored",
        "RETRY_ATTEMPTED": "retry_attempted",
        "RETRY_EXHAUSTED": "retry_exhausted",
        "ROLLBACK_PERFORMED": "rollback_performed",
        "SECURITY_IP_BLOCKED": "security_ip_blocked",
        "SECURITY_SESSION_INVALIDATED": "security_session_invalidated",
        "SECURITY_VIOLATION": "security_violation",
        "SHADOW_BUDGET_CALCULATED": "shadow_budget_calculated",
        "SHADOW_LOG_RECOVERED": "shadow_log_recovered",
        "SHADOW_LOG_SYNC_FAILED": "shadow_log_sync_failed",
        "SYSTEM_CONTROL_CHANGED": "system_control_changed",
        "WAL_CORRUPTION_DETECTED": "wal_corruption_detected",
        "WAL_RECOVERED": "wal_recovered",
        "WAL_ROTATED": "wal_rotated",
    }

    def test_all_member_names_match_snapshot(self):
        """AuditEventType member names match the frozen snapshot exactly."""
        from baldur.audit.event_buffer import AuditEventType

        actual_names = {e.name for e in AuditEventType}
        expected_names = set(self.EXPECTED_MEMBERS.keys())

        added = actual_names - expected_names
        removed = expected_names - actual_names

        assert not added, (
            f"New AuditEventType members detected: {sorted(added)}. "
            "Update EXPECTED_MEMBERS snapshot and verify downstream consumers."
        )
        assert not removed, (
            f"AuditEventType members removed: {sorted(removed)}. "
            "This breaks downstream consumers — verify before updating snapshot."
        )

    def test_all_member_values_match_snapshot(self):
        """AuditEventType member values (serialized strings) match snapshot."""
        from baldur.audit.event_buffer import AuditEventType

        actual = {e.name: e.value for e in AuditEventType}

        changed = {
            name: (self.EXPECTED_MEMBERS[name], actual[name])
            for name in self.EXPECTED_MEMBERS
            if name in actual and actual[name] != self.EXPECTED_MEMBERS[name]
        }

        assert not changed, (
            f"AuditEventType values changed: {changed}. "
            "These string values are deserialized by downstream consumers "
            "(Kafka, WAL) — renaming breaks cross-service compatibility."
        )

    def test_member_count_matches_snapshot(self):
        """AuditEventType total member count matches snapshot."""
        from baldur.audit.event_buffer import AuditEventType

        actual_count = len(AuditEventType)
        expected_count = len(self.EXPECTED_MEMBERS)

        assert actual_count == expected_count, (
            f"AuditEventType has {actual_count} members, "
            f"expected {expected_count}. "
            "Update snapshot after verifying cross-service impact."
        )

    def test_all_values_are_str_type(self):
        """All AuditEventType values are str (JSON-serializable contract)."""
        from baldur.audit.event_buffer import AuditEventType

        for member in AuditEventType:
            assert isinstance(member.value, str), (
                f"{member.name}.value is {type(member.value).__name__}, "
                "expected str for JSON/Kafka serialization."
            )

    def test_enum_inherits_str(self):
        """AuditEventType inherits from str for JSON serialization."""
        from baldur.audit.event_buffer import AuditEventType

        assert issubclass(AuditEventType, str), (
            "AuditEventType must inherit from str for automatic JSON serialization."
        )


# =============================================================================
# C. Test AuditEvent
# =============================================================================


class TestAuditEvent:
    """AuditEvent 데이터 클래스 테스트."""

    def test_create_event_with_defaults(self):
        """기본값으로 이벤트 생성."""
        from baldur.audit.event_buffer import AuditEvent, AuditEventType

        event = AuditEvent(
            event_type=AuditEventType.DLQ_STORE,
            source="TestService",
        )

        assert event.event_type == AuditEventType.DLQ_STORE
        assert event.source == "TestService"
        assert event.success is True
        assert event.actor_type == "system"
        assert isinstance(event.timestamp, datetime)

    def test_create_event_with_details(self):
        """상세 정보와 함께 이벤트 생성."""
        from baldur.audit.event_buffer import AuditEvent, AuditEventType

        event = AuditEvent(
            event_type=AuditEventType.DLQ_STORE,
            source="DLQService",
            details={"dlq_id": 123, "domain": "payment"},
            success=True,
            domain="payment",
            target_id="123",
        )

        assert event.details["dlq_id"] == 123
        assert event.domain == "payment"
        assert event.target_id == "123"

    def test_create_failed_event(self):
        """실패 이벤트 생성."""
        from baldur.audit.event_buffer import AuditEvent, AuditEventType

        event = AuditEvent(
            event_type=AuditEventType.ERROR_DETECTED,
            source="View",
            success=False,
            error_message="Internal Server Error",
        )

        assert event.success is False
        assert event.error_message == "Internal Server Error"

    def test_event_to_dict(self):
        """이벤트의 to_dict() 메서드 테스트."""
        from baldur.audit.event_buffer import AuditEvent, AuditEventType

        event = AuditEvent(
            event_type=AuditEventType.CB_STATE_CHANGE,
            source="CircuitBreaker",
            details={"cb_name": "payment", "new_state": "open"},
        )

        event_dict = event.to_dict()

        assert event_dict["event_type"] == "circuit_breaker_state_change"
        assert event_dict["source"] == "CircuitBreaker"
        assert "cb_name" in event_dict["details"]
        assert "timestamp" in event_dict

    def test_event_repr(self):
        """이벤트의 __repr__ 테스트."""
        from baldur.audit.event_buffer import AuditEvent, AuditEventType

        event = AuditEvent(
            event_type=AuditEventType.DLQ_STORE,
            source="Test",
        )

        repr_str = repr(event)
        assert "dlq_store" in repr_str
        assert "Test" in repr_str


# =============================================================================
# Test RequestAuditBuffer
# =============================================================================


class TestRequestAuditBuffer:
    """RequestAuditBuffer 클래스 테스트."""

    def test_create_empty_buffer(self):
        """빈 버퍼 생성."""
        from baldur.audit.event_buffer import RequestAuditBuffer

        buffer = RequestAuditBuffer()

        assert buffer.has_events() is False
        assert buffer.event_count() == 0
        assert buffer.request_id is None

    def test_add_event_to_buffer(self):
        """버퍼에 이벤트 추가."""
        from baldur.audit.event_buffer import AuditEventType, RequestAuditBuffer

        buffer = RequestAuditBuffer()

        event = buffer.add(
            event_type=AuditEventType.DLQ_STORE,
            source="TestService",
            details={"dlq_id": 1},
        )

        assert buffer.has_events() is True
        assert buffer.event_count() == 1
        assert event.event_type == AuditEventType.DLQ_STORE

    def test_add_multiple_events(self):
        """버퍼에 여러 이벤트 추가."""
        from baldur.audit.event_buffer import AuditEventType, RequestAuditBuffer

        buffer = RequestAuditBuffer()

        buffer.add(event_type=AuditEventType.DLQ_STORE, source="A")
        buffer.add(event_type=AuditEventType.CB_STATE_CHANGE, source="B")
        buffer.add(event_type=AuditEventType.RATE_LIMITED, source="C")

        assert buffer.event_count() == 3

    def test_add_event_with_all_params(self):
        """모든 파라미터로 이벤트 추가."""
        from baldur.audit.event_buffer import AuditEventType, RequestAuditBuffer

        buffer = RequestAuditBuffer()

        event = buffer.add(
            event_type=AuditEventType.GOVERNANCE_BLOCKED,
            source="GovernanceGuard",
            details={"action": "auto_replay"},
            actor_id="user123",
            actor_type="user",
            success=False,
            error_message="Kill switch active",
            target_type="replay_service",
            target_id="replay-1",
            domain="payment",
            reason="kill_switch_active",
        )

        assert event.actor_id == "user123"
        assert event.actor_type == "user"
        assert event.success is False
        assert event.domain == "payment"

    def test_get_events_returns_copy(self):
        """get_events()가 복사본을 반환하는지 확인."""
        from baldur.audit.event_buffer import AuditEventType, RequestAuditBuffer

        buffer = RequestAuditBuffer()
        buffer.add(event_type=AuditEventType.DLQ_STORE, source="Test")

        events = buffer.get_events()
        events.clear()  # 복사본 수정

        # 원본은 영향 없음
        assert buffer.event_count() == 1

    def test_get_events_by_type(self):
        """특정 타입의 이벤트만 가져오기."""
        from baldur.audit.event_buffer import AuditEventType, RequestAuditBuffer

        buffer = RequestAuditBuffer()
        buffer.add(event_type=AuditEventType.DLQ_STORE, source="A")
        buffer.add(event_type=AuditEventType.DLQ_STORE, source="B")
        buffer.add(event_type=AuditEventType.CB_STATE_CHANGE, source="C")

        dlq_events = buffer.get_events_by_type(AuditEventType.DLQ_STORE)

        assert len(dlq_events) == 2

    def test_get_failed_events(self):
        """실패 이벤트만 가져오기."""
        from baldur.audit.event_buffer import AuditEventType, RequestAuditBuffer

        buffer = RequestAuditBuffer()
        buffer.add(event_type=AuditEventType.DLQ_STORE, source="A", success=True)
        buffer.add(event_type=AuditEventType.ERROR_DETECTED, source="B", success=False)
        buffer.add(
            event_type=AuditEventType.GOVERNANCE_BLOCKED, source="C", success=False
        )

        failed_events = buffer.get_failed_events()

        assert len(failed_events) == 2

    def test_set_request_metadata(self):
        """요청 메타데이터 설정."""
        from baldur.audit.event_buffer import RequestAuditBuffer

        buffer = RequestAuditBuffer()
        buffer.set_request_metadata(
            path="/api/test/",
            method="POST",
            user_id="user123",
        )

        assert buffer._path == "/api/test/"
        assert buffer._method == "POST"
        assert buffer._user_id == "user123"

    def test_get_elapsed_seconds(self):
        """경과 시간 계산."""
        import time

        from baldur.audit.event_buffer import RequestAuditBuffer

        buffer = RequestAuditBuffer()
        time.sleep(0.01)  # 10ms

        elapsed = buffer.get_elapsed_seconds()

        assert elapsed >= 0.01

    def test_buffer_to_dict(self):
        """버퍼 전체를 딕셔너리로 변환."""
        from baldur.audit.event_buffer import AuditEventType, RequestAuditBuffer

        buffer = RequestAuditBuffer()
        buffer.request_id = "test-request-123"
        buffer.add(event_type=AuditEventType.DLQ_STORE, source="Test")

        buffer_dict = buffer.to_dict()

        assert buffer_dict["request_id"] == "test-request-123"
        assert buffer_dict["event_count"] == 1
        assert len(buffer_dict["events"]) == 1
        assert "elapsed_seconds" in buffer_dict

    def test_get_or_create_with_mock_request(self):
        """Mock request에서 버퍼 가져오기/생성."""
        from baldur.audit.event_buffer import RequestAuditBuffer

        # Mock Django request
        mock_request = MagicMock()
        mock_request.META = {}

        # 첫 번째 호출: 생성
        buffer1 = RequestAuditBuffer.get_or_create(mock_request)
        assert buffer1 is not None

        # 두 번째 호출: 기존 버퍼 반환
        buffer2 = RequestAuditBuffer.get_or_create(mock_request)
        assert buffer1 is buffer2

    def test_get_returns_none_when_no_buffer(self):
        """버퍼가 없으면 None 반환."""
        from baldur.audit.event_buffer import RequestAuditBuffer

        mock_request = MagicMock()
        mock_request.META = {}

        result = RequestAuditBuffer.get(mock_request)

        assert result is None

    def test_exists_check(self):
        """버퍼 존재 여부 확인."""
        from baldur.audit.event_buffer import RequestAuditBuffer

        mock_request = MagicMock()
        mock_request.META = {}

        assert RequestAuditBuffer.exists(mock_request) is False

        RequestAuditBuffer.get_or_create(mock_request)

        assert RequestAuditBuffer.exists(mock_request) is True

    def test_clear_buffer(self):
        """버퍼 초기화."""
        from baldur.audit.event_buffer import AuditEventType, RequestAuditBuffer

        buffer = RequestAuditBuffer()
        buffer.add(event_type=AuditEventType.DLQ_STORE, source="Test")
        buffer.add(event_type=AuditEventType.CB_STATE_CHANGE, source="Test")

        assert buffer.event_count() == 2

        buffer.clear()

        assert buffer.event_count() == 0

    def test_get_or_create_without_meta(self):
        """META 속성이 없는 request 처리."""
        from baldur.audit.event_buffer import RequestAuditBuffer

        # META 속성이 없는 객체
        mock_request = MagicMock(spec=[])  # META 없음

        buffer = RequestAuditBuffer.get_or_create(mock_request)

        # 새 버퍼가 생성됨
        assert buffer is not None
        assert isinstance(buffer, RequestAuditBuffer)


# =============================================================================
# Test add_audit_event Function
# =============================================================================


class TestAddAuditEventFunction:
    """add_audit_event 편의 함수 테스트."""

    def test_add_audit_event_to_request(self):
        """request에 이벤트 추가."""
        from baldur.audit.event_buffer import (
            AuditEventType,
            RequestAuditBuffer,
            add_audit_event,
        )

        mock_request = MagicMock()
        mock_request.META = {}

        event = add_audit_event(
            mock_request,
            AuditEventType.DLQ_STORE,
            "TestService",
            details={"test": True},
        )

        assert event is not None
        assert event.event_type == AuditEventType.DLQ_STORE

        # 버퍼에 추가되었는지 확인
        buffer = RequestAuditBuffer.get(mock_request)
        assert buffer.event_count() == 1

    def test_add_audit_event_multiple_times(self):
        """여러 번 이벤트 추가."""
        from baldur.audit.event_buffer import (
            AuditEventType,
            RequestAuditBuffer,
            add_audit_event,
        )

        mock_request = MagicMock()
        mock_request.META = {}

        add_audit_event(mock_request, AuditEventType.DLQ_STORE, "A")
        add_audit_event(mock_request, AuditEventType.CB_STATE_CHANGE, "B")
        add_audit_event(mock_request, AuditEventType.ERROR_DETECTED, "C")

        buffer = RequestAuditBuffer.get(mock_request)
        assert buffer.event_count() == 3


# =============================================================================
# Test audit_helpers Hybrid Logic (No Django required)
# =============================================================================


class TestAuditHelpersHybridNoRequest:
    """audit_helpers 하이브리드 로직 테스트 (request 없음)."""

    @pytest.fixture(autouse=True)
    def _require_pro(self):
        pytest.importorskip("baldur_pro")

    def test_log_dlq_store_without_request(self):
        """request 없이 호출 - 직접 로깅."""
        from baldur_pro.services.audit import log_dlq_store_audit

        # request 없이 호출 - 에러 없이 완료되어야 함
        log_dlq_store_audit(
            dlq_id=456,
            domain="point",
            failure_type="TIMEOUT",
        )
        # 에러 없이 완료되면 성공

    def test_log_dlq_replay_without_request(self):
        """request 없이 호출 - 직접 로깅."""
        from baldur_pro.services.audit import log_dlq_replay_audit

        log_dlq_replay_audit(
            dlq_id=789,
            domain="payment",
            success=True,
        )
        # 에러 없이 완료되면 성공

    def test_log_cb_state_change_without_request(self):
        """request 없이 호출 - 직접 로깅."""
        from baldur_pro.services.audit import log_cb_state_change_audit

        log_cb_state_change_audit(
            cb_name="payment_cb",
            old_state="closed",
            new_state="open",
        )
        # 에러 없이 완료되면 성공

    def test_log_governance_blocked_without_request(self):
        """request 없이 호출 - 직접 로깅."""
        from baldur_pro.services.audit import log_governance_blocked_audit

        log_governance_blocked_audit(
            action="auto_replay",
            block_reason="kill_switch_active",
        )
        # 에러 없이 완료되면 성공

    def test_log_rate_limited_without_request(self):
        """request 없이 호출 - 직접 로깅."""
        from baldur_pro.services.audit import log_rate_limited_audit

        log_rate_limited_audit(
            client_ip="192.168.1.100",
            endpoint="/api/test/",
            limit_type="global",
        )
        # 에러 없이 완료되면 성공

    def test_log_pool_cb_rejection_without_request(self):
        """request 없이 호출 - 직접 로깅."""
        from baldur_pro.services.audit import log_pool_cb_rejection_audit

        log_pool_cb_rejection_audit(
            pool_name="default",
            current_utilization=0.95,
            threshold=0.90,
        )
        # 에러 없이 완료되면 성공


# =============================================================================
# Test API Exception EventTypes
# =============================================================================


class TestAPIExceptionEventTypes:
    """API 예외 관련 AuditEventType 테스트."""

    def test_api_exception_event_types_exist(self):
        """API 예외 관련 이벤트 타입이 존재하는지 확인."""
        from baldur.audit.event_buffer import AuditEventType

        required_types = [
            "API_EXCEPTION",
            "API_VALIDATION_ERROR",
            "API_AUTH_ERROR",
        ]

        for type_name in required_types:
            assert hasattr(AuditEventType, type_name), f"{type_name} should exist"

    def test_api_exception_event_type_values(self):
        """API 예외 이벤트 타입 값이 올바른지 확인."""
        from baldur.audit.event_buffer import AuditEventType

        assert AuditEventType.API_EXCEPTION.value == "api_exception"
        assert AuditEventType.API_VALIDATION_ERROR.value == "api_validation_error"
        assert AuditEventType.API_AUTH_ERROR.value == "api_auth_error"

    def test_create_api_exception_event(self):
        """API 예외 이벤트 생성 테스트."""
        from baldur.audit.event_buffer import AuditEvent, AuditEventType

        event = AuditEvent(
            event_type=AuditEventType.API_EXCEPTION,
            source="ExceptionHandler",
            details={
                "error_code": "SYSTEM_INTERNAL_ERROR",
                "exception_class": "RuntimeError",
                "path": "/api/test/",
                "method": "POST",
            },
            success=False,
            error_message="Internal server error",
        )

        assert event.event_type == AuditEventType.API_EXCEPTION
        assert event.source == "ExceptionHandler"
        assert event.success is False
        assert event.details["error_code"] == "SYSTEM_INTERNAL_ERROR"

    def test_create_api_validation_error_event(self):
        """API 검증 오류 이벤트 생성 테스트."""
        from baldur.audit.event_buffer import AuditEvent, AuditEventType

        event = AuditEvent(
            event_type=AuditEventType.API_VALIDATION_ERROR,
            source="ExceptionHandler",
            details={
                "error_code": "VALIDATION_FIELD_REQUIRED",
                "exception_class": "ValidationError",
                "field": "amount",
            },
            success=False,
        )

        assert event.event_type == AuditEventType.API_VALIDATION_ERROR
        assert event.details["field"] == "amount"

    def test_create_api_auth_error_event(self):
        """API 인증 오류 이벤트 생성 테스트."""
        from baldur.audit.event_buffer import AuditEvent, AuditEventType

        event = AuditEvent(
            event_type=AuditEventType.API_AUTH_ERROR,
            source="ExceptionHandler",
            details={
                "error_code": "AUTH_TOKEN_EXPIRED",
                "exception_class": "AuthenticationFailed",
            },
            success=False,
            error_message="Token has expired",
        )

        assert event.event_type == AuditEventType.API_AUTH_ERROR
        assert event.details["exception_class"] == "AuthenticationFailed"


# =============================================================================
# Test has_event_from_source
# =============================================================================


class TestHasEventFromSource:
    """RequestAuditBuffer.has_event_from_source() 메서드 테스트."""

    def test_has_event_from_source_when_exists(self):
        """특정 source의 이벤트가 존재할 때 True 반환."""
        from baldur.audit.event_buffer import AuditEventType, RequestAuditBuffer

        buffer = RequestAuditBuffer()
        buffer.add(
            event_type=AuditEventType.API_EXCEPTION,
            source="ExceptionHandler",
            details={"path": "/api/test/"},
        )

        assert buffer.has_event_from_source("ExceptionHandler") is True

    def test_has_event_from_source_when_not_exists(self):
        """특정 source의 이벤트가 없을 때 False 반환."""
        from baldur.audit.event_buffer import AuditEventType, RequestAuditBuffer

        buffer = RequestAuditBuffer()
        buffer.add(
            event_type=AuditEventType.DLQ_STORE,
            source="DLQService",
        )

        assert buffer.has_event_from_source("ExceptionHandler") is False

    def test_has_event_from_source_empty_buffer(self):
        """빈 버퍼에서 False 반환."""
        from baldur.audit.event_buffer import RequestAuditBuffer

        buffer = RequestAuditBuffer()

        assert buffer.has_event_from_source("AnySource") is False

    def test_has_event_from_source_multiple_sources(self):
        """여러 source가 있을 때 특정 source만 확인."""
        from baldur.audit.event_buffer import AuditEventType, RequestAuditBuffer

        buffer = RequestAuditBuffer()
        buffer.add(event_type=AuditEventType.DLQ_STORE, source="DLQService")
        buffer.add(event_type=AuditEventType.CB_STATE_CHANGE, source="CircuitBreaker")
        buffer.add(event_type=AuditEventType.API_EXCEPTION, source="ExceptionHandler")

        assert buffer.has_event_from_source("ExceptionHandler") is True
        assert buffer.has_event_from_source("DLQService") is True
        assert buffer.has_event_from_source("AuditMiddleware") is False

    def test_has_event_from_source_case_sensitive(self):
        """source 검색이 대소문자를 구분하는지 확인."""
        from baldur.audit.event_buffer import AuditEventType, RequestAuditBuffer

        buffer = RequestAuditBuffer()
        buffer.add(
            event_type=AuditEventType.API_EXCEPTION,
            source="ExceptionHandler",
        )

        assert buffer.has_event_from_source("ExceptionHandler") is True
        assert buffer.has_event_from_source("exceptionhandler") is False
        assert buffer.has_event_from_source("EXCEPTIONHANDLER") is False
