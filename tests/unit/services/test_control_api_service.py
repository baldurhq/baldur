"""
Tests for services/control_api_service.py - Control API Service.
서비스 차단/허용, 장애 주입, 위험 평가 등 Control API 핵심 비즈니스 로직 단위 테스트.

커버리지 대상:
- ReasonClassification enum
- classify_reason() 함수
- assess_risk_level() 함수
- ControlRequest / ControlResponse 데이터클래스
- ControlAPIService.execute() 라우팅
- _validate_request() (inject 금지, override TTL 검증)
- _execute_allow/block/override/reset/inject_failure/inject_success()
- _gather_evidence(), _record_audit()
- get_status(), get_service_status()
- is_failure_injection_active(), get_failure_injection_config()
- 싱글톤 get_control_api_service()
"""

from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from baldur.core.constants import (
    ControlAPIActions,
    ControlAPIEnvironments,
    RiskLevels,
)
from baldur.services.control_api_service import (
    ControlAPIService,
    ControlRequest,
    ControlResponse,
    ReasonClassification,
    assess_risk_level,
    classify_reason,
    get_control_api_service,
)

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def mock_cb_service():
    """CircuitBreakerService mock."""
    cb = MagicMock()
    # force_close / force_open 결과
    success_result = MagicMock()
    success_result.success = True
    success_result.error = None
    cb.force_close.return_value = success_result
    cb.force_open.return_value = success_result

    # get_or_create_state 결과
    state = MagicMock()
    state.state = "closed"
    state.failure_count = 0
    state.success_count = 0
    state.last_failure_at = None
    state.manually_controlled = False
    state.control_reason = None
    cb.get_or_create_state.return_value = state
    cb.get_all_states.return_value = {}
    return cb


@pytest.fixture
def service(mock_cb_service):
    """ControlAPIService 인스턴스 (의존성 모킹)."""
    with (
        patch(
            "baldur.services.circuit_breaker.get_circuit_breaker_service",
            return_value=mock_cb_service,
        ),
        patch(
            "baldur.services.replay_service.ReplayService",
            return_value=MagicMock(),
        ),
    ):
        svc = ControlAPIService()
    return svc


def _make_request(**overrides) -> ControlRequest:
    """ControlRequest 팩토리."""
    defaults = {
        "service_name": "payment",
        "action": ControlAPIActions.ALLOW,
        "reason": "Test reason",
        "environment": ControlAPIEnvironments.TEST,
    }
    defaults.update(overrides)
    return ControlRequest(**defaults)


# =============================================================================
# ReasonClassification Tests
# =============================================================================


class TestReasonClassification:
    """ReasonClassification Enum 테스트."""

    def test_enum_values(self):
        """Enum values
        모든 ReasonClassification 멤버가 올바른 문자열 값을 갖는지 확인.
        """
        assert (
            ReasonClassification.EXTERNAL_DEPENDENCY_FAILURE
            == "external-dependency-failure"
        )
        assert ReasonClassification.CHAOS_EXPERIMENT == "chaos-experiment"
        assert ReasonClassification.UNKNOWN == "unknown"

    def test_enum_is_string(self):
        """Enum is str subclass
        Enum 값이 str 타입인지 확인.
        """
        assert isinstance(ReasonClassification.MAINTENANCE_WINDOW.value, str)


# =============================================================================
# classify_reason Tests
# =============================================================================


class TestClassifyReason:
    """classify_reason() 함수 테스트."""

    @pytest.mark.parametrize(
        ("reason", "expected"),
        [
            (
                "Scheduled maintenance window",
                ReasonClassification.MAINTENANCE_WINDOW.value,
            ),
            (
                "Upgrade deployment in progress",
                ReasonClassification.MAINTENANCE_WINDOW.value,
            ),
            ("SLA breach detected", ReasonClassification.SLA_BREACH_MITIGATION.value),
            (
                "Threshold violation alert",
                ReasonClassification.SLA_BREACH_MITIGATION.value,
            ),
            ("Chaos experiment running", ReasonClassification.CHAOS_EXPERIMENT.value),
            ("Resilience test started", ReasonClassification.CHAOS_EXPERIMENT.value),
            (
                "Service recovered from outage",
                ReasonClassification.RECOVERY_PROCEDURE.value,
            ),
            ("Fixed the broken service", ReasonClassification.RECOVERY_PROCEDURE.value),
            ("Security attack detected", ReasonClassification.SECURITY_INCIDENT.value),
            ("DDoS mitigation activated", ReasonClassification.SECURITY_INCIDENT.value),
            (
                "External API down",
                ReasonClassification.EXTERNAL_DEPENDENCY_FAILURE.value,
            ),
            ("PG timeout", ReasonClassification.EXTERNAL_DEPENDENCY_FAILURE.value),
            (
                "Internal service error",
                ReasonClassification.INTERNAL_SERVICE_ERROR.value,
            ),
        ],
    )
    def test_pattern_matching(self, reason, expected):
        """Pattern matching for reason classification
        다양한 사유 문자열에 대해 올바른 분류가 반환되는지 확인.
        """
        assert classify_reason(reason) == expected

    def test_case_insensitive(self):
        """Case insensitive classification
        대소문자 구분 없이 분류가 동작하는지 확인.
        """
        assert (
            classify_reason("SCHEDULED MAINTENANCE")
            == ReasonClassification.MAINTENANCE_WINDOW.value
        )

    def test_no_matching_pattern(self):
        """No matching pattern returns manual_intervention
        매칭되는 패턴이 없을 때 'manual-intervention'을 반환하는지 확인.
        """
        assert (
            classify_reason("Some random reason")
            == ReasonClassification.MANUAL_INTERVENTION.value
        )

    def test_empty_reason(self):
        """Empty reason string
        빈 문자열에 대해 'manual-intervention'을 반환하는지 확인.
        """
        assert classify_reason("") == ReasonClassification.MANUAL_INTERVENTION.value


# =============================================================================
# assess_risk_level Tests
# =============================================================================


class TestAssessRiskLevel:
    """assess_risk_level() 함수 테스트."""

    def test_allow_in_test(self):
        """Allow in test environment
        테스트 환경의 allow 액션은 INFO 수준인지 확인.
        """
        assert (
            assess_risk_level(ControlAPIActions.ALLOW, ControlAPIEnvironments.TEST)
            == RiskLevels.INFO
        )

    def test_block_in_ops(self):
        """Block in ops environment
        운영 환경의 block 액션은 HIGH 수준인지 확인.
        """
        assert (
            assess_risk_level(ControlAPIActions.BLOCK, ControlAPIEnvironments.OPS)
            == RiskLevels.HIGH
        )

    def test_override_in_ops(self):
        """Override in ops environment
        운영 환경의 override 액션은 CRITICAL 수준인지 확인.
        """
        assert (
            assess_risk_level(ControlAPIActions.OVERRIDE, ControlAPIEnvironments.OPS)
            == RiskLevels.CRITICAL
        )

    def test_inject_failure_in_ops(self):
        """Inject failure in ops environment
        운영 환경의 inject_failure 액션은 FORBIDDEN 수준인지 확인.
        """
        assert (
            assess_risk_level(
                ControlAPIActions.INJECT_FAILURE, ControlAPIEnvironments.OPS
            )
            == RiskLevels.FORBIDDEN
        )

    def test_inject_success_in_chaos(self):
        """Inject success in chaos
        카오스 환경의 inject_success 액션은 INFO 수준인지 확인.
        """
        assert (
            assess_risk_level(
                ControlAPIActions.INJECT_SUCCESS, ControlAPIEnvironments.CHAOS
            )
            == RiskLevels.INFO
        )

    def test_unknown_combination_defaults_warning(self):
        """Unknown combination defaults to WARNING
        정의되지 않은 조합은 WARNING을 반환하는지 확인.
        """
        assert assess_risk_level("unknown_action", "unknown_env") == RiskLevels.WARNING


# =============================================================================
# ControlRequest / ControlResponse Tests
# =============================================================================


class TestControlRequest:
    """ControlRequest 데이터클래스 테스트."""

    def test_default_values(self):
        """Default values
        기본값이 올바르게 설정되는지 확인.
        """
        req = ControlRequest(
            service_name="payment",
            action="allow",
            reason="test",
            environment="test",
        )
        assert req.ttl_minutes is None
        assert req.metadata == {}
        assert req.actor == "system"
        assert req.actor_role == "automation"
        assert req.request_id  # UUID가 자동 생성

    def test_custom_values(self):
        """Custom values
        커스텀 값이 올바르게 설정되는지 확인.
        """
        req = ControlRequest(
            service_name="payment",
            action="block",
            reason="PG down",
            environment="ops",
            ttl_minutes=30,
            actor="admin",
            metadata={"trigger_replay": True},
        )
        assert req.ttl_minutes == 30
        assert req.actor == "admin"
        assert req.metadata["trigger_replay"] is True


class TestControlResponse:
    """ControlResponse 데이터클래스 테스트."""

    def test_to_dict_minimal(self):
        """Minimal to_dict
        필수 필드만 있을 때 to_dict()가 올바르게 동작하는지 확인.
        """
        resp = ControlResponse(status="success", action_applied="allow")
        d = resp.to_dict()
        assert d["status"] == "success"
        assert d["action_applied"] == "allow"
        assert "correlation_id" in d
        # 빈 optional 필드가 제외되는지 확인
        assert "system_state" not in d
        assert "error_code" not in d

    def test_to_dict_full(self):
        """Full to_dict
        모든 필드가 채워졌을 때 to_dict()가 올바르게 동작하는지 확인.
        """
        resp = ControlResponse(
            status="error",
            action_applied="block",
            system_state="block",
            effective_until="2025-01-01T00:00:00Z",
            reason_classification="maintenance-window",
            evidence={"failure_count": 5},
            error_code="TEST_ERROR",
            error_message="Test error",
            risk_level="high",
        )
        d = resp.to_dict()
        assert d["system_state"] == "block"
        assert d["effective_until"] == "2025-01-01T00:00:00Z"
        assert d["evidence"]["failure_count"] == 5
        assert d["error_code"] == "TEST_ERROR"
        assert d["risk_level"] == "high"


# =============================================================================
# ControlAPIService execute Tests
# =============================================================================


class TestExecuteRouting:
    """ControlAPIService.execute() 액션 라우팅 테스트."""

    def test_execute_allow(self, service):
        """Execute allow action
        allow 액션이 올바르게 실행되는지 확인.
        """
        req = _make_request(action=ControlAPIActions.ALLOW)
        resp = service.execute(req)
        assert resp.status == "success"
        assert resp.action_applied == "allow"
        assert resp.system_state == "allow"

    def test_execute_block(self, service):
        """Execute block action
        block 액션이 올바르게 실행되는지 확인.
        """
        req = _make_request(action=ControlAPIActions.BLOCK)
        resp = service.execute(req)
        assert resp.status == "success"
        assert resp.action_applied == "block"

    def test_execute_override(self, service):
        """Execute override action
        override 액션이 올바르게 실행되는지 확인.
        """
        req = _make_request(action=ControlAPIActions.OVERRIDE, ttl_minutes=30)
        resp = service.execute(req)
        assert resp.status == "success"
        assert resp.action_applied == "override"

    def test_execute_reset(self, service):
        """Execute reset action
        reset 액션이 올바르게 실행되는지 확인.
        """
        req = _make_request(action=ControlAPIActions.RESET)
        resp = service.execute(req)
        assert resp.status == "success"
        assert resp.action_applied == "reset"

    def test_execute_inject_failure(self, service):
        """Execute inject_failure action
        inject_failure 액션이 올바르게 실행되는지 확인.
        """
        req = _make_request(action=ControlAPIActions.INJECT_FAILURE)
        resp = service.execute(req)
        assert resp.status == "success"
        assert resp.action_applied == "inject_failure"

    def test_execute_inject_success(self, service):
        """Execute inject_success action
        inject_success 액션이 올바르게 실행되는지 확인.
        """
        req = _make_request(action=ControlAPIActions.INJECT_SUCCESS)
        resp = service.execute(req)
        assert resp.status == "success"
        assert resp.action_applied == "inject_success"

    def test_execute_unknown_action(self, service):
        """Execute unknown action
        알 수 없는 액션에 대해 error 응답을 반환하는지 확인.
        """
        req = _make_request(action="unknown_action")
        resp = service.execute(req)
        assert resp.status == "error"
        assert resp.error_code == "UNKNOWN_ACTION"

    def test_execute_exception_handling(self, service, mock_cb_service):
        """Execute with exception
        실행 중 예외 발생 시 error 응답을 반환하는지 확인.
        """
        mock_cb_service.force_close.side_effect = RuntimeError("Unexpected error")
        req = _make_request(action=ControlAPIActions.ALLOW)
        resp = service.execute(req)
        assert resp.status == "error"
        assert resp.error_code == "EXECUTION_ERROR"

    def test_execute_adds_metadata(self, service):
        """Execute adds classification and risk
        실행 후 reason_classification과 risk_level이 추가되는지 확인.
        """
        req = _make_request(
            action=ControlAPIActions.ALLOW,
            reason="Scheduled maintenance",
        )
        resp = service.execute(req)
        assert (
            resp.reason_classification == ReasonClassification.MAINTENANCE_WINDOW.value
        )
        assert resp.risk_level == RiskLevels.INFO
        assert resp.correlation_id == req.request_id


# =============================================================================
# _validate_request Tests
# =============================================================================


class TestValidateRequest:
    """_validate_request() 검증 테스트."""

    def test_inject_failure_forbidden_in_ops(self, service):
        """Inject failure forbidden in ops
        운영 환경에서 inject_failure가 거부되는지 확인.
        """
        req = _make_request(
            action=ControlAPIActions.INJECT_FAILURE,
            environment=ControlAPIEnvironments.OPS,
        )
        result = service._validate_request(req)
        assert result is not None
        assert result.status == "rejected"
        assert result.error_code == "ACTION_FORBIDDEN_IN_ENVIRONMENT"

    def test_inject_success_forbidden_in_ops(self, service):
        """Inject success forbidden in ops
        운영 환경에서 inject_success가 거부되는지 확인.
        """
        req = _make_request(
            action=ControlAPIActions.INJECT_SUCCESS,
            environment=ControlAPIEnvironments.OPS,
        )
        result = service._validate_request(req)
        assert result is not None
        assert result.error_code == "ACTION_FORBIDDEN_IN_ENVIRONMENT"

    def test_override_requires_ttl_in_ops(self, service):
        """Override requires TTL in ops
        운영 환경에서 override 시 TTL이 필수인지 확인.
        """
        req = _make_request(
            action=ControlAPIActions.OVERRIDE,
            environment=ControlAPIEnvironments.OPS,
            ttl_minutes=None,
        )
        result = service._validate_request(req)
        assert result is not None
        assert result.error_code == "TTL_REQUIRED_FOR_OPS_OVERRIDE"

    def test_override_ttl_limit_in_ops(self, service):
        """Override TTL exceeds limit in ops
        운영 환경에서 override TTL이 60분을 초과하면 거부되는지 확인.
        """
        req = _make_request(
            action=ControlAPIActions.OVERRIDE,
            environment=ControlAPIEnvironments.OPS,
            ttl_minutes=90,
        )
        result = service._validate_request(req)
        assert result is not None
        assert result.error_code == "TTL_EXCEEDS_OPS_LIMIT"

    def test_override_valid_ttl_in_ops(self, service):
        """Override valid TTL in ops
        운영 환경에서 유효한 TTL(60분 이하)이면 None(유효)을 반환하는지 확인.
        """
        req = _make_request(
            action=ControlAPIActions.OVERRIDE,
            environment=ControlAPIEnvironments.OPS,
            ttl_minutes=30,
        )
        result = service._validate_request(req)
        assert result is None

    def test_allow_in_ops_valid(self, service):
        """Allow in ops is valid
        운영 환경에서 allow 액션은 검증을 통과하는지 확인.
        """
        req = _make_request(
            action=ControlAPIActions.ALLOW,
            environment=ControlAPIEnvironments.OPS,
        )
        result = service._validate_request(req)
        assert result is None

    def test_inject_failure_in_chaos_valid(self, service):
        """Inject failure in chaos is valid
        카오스 환경에서 inject_failure는 검증을 통과하는지 확인.
        """
        req = _make_request(
            action=ControlAPIActions.INJECT_FAILURE,
            environment=ControlAPIEnvironments.CHAOS,
        )
        result = service._validate_request(req)
        assert result is None


# =============================================================================
# Action Implementation Tests
# =============================================================================


class TestExecuteAllow:
    """_execute_allow() 테스트."""

    def test_allow_calls_force_close(self, service, mock_cb_service):
        """Allow calls force_close
        allow 액션이 circuit_breaker.force_close()를 호출하는지 확인.
        """
        req = _make_request(action=ControlAPIActions.ALLOW)
        service._execute_allow(req)
        mock_cb_service.force_close.assert_called_once()

    def test_allow_failure(self, service, mock_cb_service):
        """Allow failure response
        force_close 실패 시 error 응답을 반환하는지 확인.
        """
        fail_result = MagicMock()
        fail_result.success = False
        fail_result.error = "CB Error"
        mock_cb_service.force_close.return_value = fail_result

        req = _make_request(action=ControlAPIActions.ALLOW)
        resp = service._execute_allow(req)
        assert resp.status == "error"
        assert resp.error_code == "CIRCUIT_BREAKER_ERROR"


class TestExecuteBlock:
    """_execute_block() 테스트."""

    def test_block_calls_force_open(self, service, mock_cb_service):
        """Block calls force_open
        block 액션이 circuit_breaker.force_open()을 호출하는지 확인.
        """
        req = _make_request(action=ControlAPIActions.BLOCK)
        service._execute_block(req)
        mock_cb_service.force_open.assert_called_once()

    def test_block_with_ttl(self, service):
        """Block with TTL
        TTL이 있을 때 effective_until이 설정되는지 확인.
        """
        req = _make_request(action=ControlAPIActions.BLOCK, ttl_minutes=30)
        resp = service._execute_block(req)
        assert resp.effective_until is not None

    def test_block_ops_default_ttl(self, service):
        """Block in ops gets default 90min TTL
        운영 환경에서 TTL 미지정 시 기본 90분이 적용되는지 확인.
        """
        req = _make_request(
            action=ControlAPIActions.BLOCK,
            environment=ControlAPIEnvironments.OPS,
            ttl_minutes=None,
        )
        resp = service._execute_block(req)
        assert resp.effective_until is not None

    def test_block_failure(self, service, mock_cb_service):
        """Block failure response
        force_open 실패 시 error 응답을 반환하는지 확인.
        """
        fail_result = MagicMock()
        fail_result.success = False
        fail_result.error = "CB Error"
        mock_cb_service.force_open.return_value = fail_result

        req = _make_request(action=ControlAPIActions.BLOCK)
        resp = service._execute_block(req)
        assert resp.status == "error"


class TestExecuteOverride:
    """_execute_override() 테스트."""

    def test_override_success(self, service):
        """Override success
        override 성공 시 올바른 응답을 반환하는지 확인.
        """
        req = _make_request(action=ControlAPIActions.OVERRIDE, ttl_minutes=15)
        resp = service._execute_override(req)
        assert resp.status == "success"
        assert resp.action_applied == "override"
        assert resp.system_state == "allow"
        assert resp.effective_until is not None

    def test_override_failure(self, service, mock_cb_service):
        """Override failure
        override 실패 시 error 응답을 반환하는지 확인.
        """
        fail_result = MagicMock()
        fail_result.success = False
        fail_result.error = "Override failed"
        mock_cb_service.force_close.return_value = fail_result

        req = _make_request(action=ControlAPIActions.OVERRIDE)
        resp = service._execute_override(req)
        assert resp.status == "error"
        assert resp.error_code == "OVERRIDE_ERROR"


class TestExecuteReset:
    """_execute_reset() 테스트."""

    def test_reset_success(self, service, mock_cb_service):
        """Reset success
        reset 성공 시 올바른 응답을 반환하는지 확인.
        """
        req = _make_request(action=ControlAPIActions.RESET)
        resp = service._execute_reset(req)
        assert resp.status == "success"
        assert resp.action_applied == "reset"
        assert resp.system_state == "allow"

    def test_reset_fallback_when_no_method(self, service, mock_cb_service):
        """Reset fallback when reset_to_default doesn't exist
        reset_to_default가 없을 때 force_close로 폴백하는지 확인.
        """
        mock_cb_service.reset_to_default.side_effect = AttributeError()
        req = _make_request(action=ControlAPIActions.RESET)
        resp = service._execute_reset(req)
        assert resp.status == "success"
        mock_cb_service.force_close.assert_called()


class TestExecuteInjectFailure:
    """_execute_inject_failure() 테스트."""

    def test_config_mode(self, service):
        """Configuration mode injection
        설정 모드 장애 주입이 올바르게 동작하는지 확인.
        """
        req = _make_request(
            action=ControlAPIActions.INJECT_FAILURE,
            metadata={"failure_rate": 0.5, "failure_type": "timeout"},
        )
        resp = service._execute_inject_failure(req)
        assert resp.status == "success"
        assert resp.evidence["failure_rate"] == 0.5
        assert resp.evidence["failure_type"] == "timeout"
        # 내부 상태에도 저장되었는지 확인
        assert service.is_failure_injection_active("payment")

    def test_trigger_cb_mode(self, service, mock_cb_service):
        """Trigger CB failures mode
        trigger_cb_failures 모드에서 record_failure가 호출되는지 확인.
        """
        state = MagicMock()
        state.state = "open"
        state.failure_count = 5
        state.manually_controlled = False
        mock_cb_service.get_or_create_state.return_value = state

        req = _make_request(
            action=ControlAPIActions.INJECT_FAILURE,
            metadata={"trigger_cb_failures": 5},
        )
        resp = service._execute_inject_failure(req)
        assert resp.status == "success"
        assert mock_cb_service.record_failure.call_count == 5
        assert resp.evidence["failures_triggered"] == 5

    def test_config_mode_with_ttl(self, service):
        """Configuration mode with TTL
        TTL 설정 시 expires_at가 설정되는지 확인.
        """
        req = _make_request(
            action=ControlAPIActions.INJECT_FAILURE,
            ttl_minutes=10,
        )
        resp = service._execute_inject_failure(req)
        assert resp.effective_until is not None


class TestExecuteInjectSuccess:
    """_execute_inject_success() 테스트."""

    def test_inject_success(self, service, mock_cb_service):
        """Inject success records successes
        inject_success가 record_success를 호출하는지 확인.
        """
        req = _make_request(
            action=ControlAPIActions.INJECT_SUCCESS,
            metadata={"success_count": 3},
        )
        resp = service._execute_inject_success(req)
        assert resp.status == "success"
        assert mock_cb_service.record_success.call_count == 3

    def test_default_success_count(self, service, mock_cb_service):
        """Default success count is 1
        metadata에 success_count가 없으면 기본값 1이 사용되는지 확인.
        """
        req = _make_request(action=ControlAPIActions.INJECT_SUCCESS)
        service._execute_inject_success(req)
        assert mock_cb_service.record_success.call_count == 1


# =============================================================================
# Helper Method Tests
# =============================================================================


class TestGatherEvidence:
    """_gather_evidence() 테스트."""

    def test_gather_evidence_success(self, service, mock_cb_service):
        """Gather evidence success
        서킷 브레이커 상태에서 evidence를 올바르게 수집하는지 확인.
        """
        evidence = service._gather_evidence("payment")
        assert "failure_count" in evidence
        assert "success_count" in evidence

    def test_gather_evidence_exception(self, service, mock_cb_service):
        """Gather evidence with exception
        예외 발생 시 빈 딕셔너리를 반환하는지 확인.
        """
        mock_cb_service.get_or_create_state.side_effect = Exception("Error")
        evidence = service._gather_evidence("payment")
        assert evidence == {}


class TestRecordAudit:
    """_record_audit() 테스트."""

    def test_record_audit_no_exception(self, service):
        """Record audit does not raise
        audit 기록 중 예외가 발생해도 전파되지 않는지 확인.
        """
        req = _make_request()
        resp = ControlResponse(status="success", action_applied="allow")
        # 예외 없이 완료되어야 함
        service._record_audit(req, resp)


# =============================================================================
# Query Method Tests
# =============================================================================


class TestGetStatus:
    """get_status() 테스트."""

    def test_get_status(self, service):
        """Get status returns expected structure
        get_status()가 올바른 구조의 딕셔너리를 반환하는지 확인.
        """
        result = service.get_status(environment="test")
        assert "services" in result
        assert result["environment"] == "test"
        assert "timestamp" in result


class TestGetServiceStatus:
    """get_service_status() 테스트."""

    def test_get_service_status(self, service):
        """Get service status
        get_service_status()가 서비스 상태를 올바르게 반환하는지 확인.
        """
        result = service.get_service_status("payment")
        assert result["service_name"] == "payment"
        assert "state" in result
        assert "failure_count" in result


# =============================================================================
# Failure Injection State Tests
# =============================================================================


class TestFailureInjectionState:
    """is_failure_injection_active / get_failure_injection_config 테스트."""

    def test_no_injection_active(self, service):
        """No injection active
        주입이 없을 때 False를 반환하는지 확인.
        """
        assert service.is_failure_injection_active("payment") is False

    def test_injection_active(self, service):
        """Injection active
        주입이 활성화된 후 True를 반환하는지 확인.
        """
        service._failure_injections["payment"] = {"enabled": True}
        assert service.is_failure_injection_active("payment") is True

    def test_injection_disabled(self, service):
        """Injection disabled
        enabled=False일 때 False를 반환하는지 확인.
        """
        service._failure_injections["payment"] = {"enabled": False}
        assert service.is_failure_injection_active("payment") is False

    def test_injection_expired(self, service):
        """Injection expired
        만료 시간이 지난 주입은 False를 반환하고 제거되는지 확인.
        """
        past = datetime.now() - timedelta(hours=1)
        service._failure_injections["payment"] = {
            "enabled": True,
            "expires_at": past,
        }
        with patch(
            "baldur.services.control_api_service.now", return_value=datetime.now()
        ):
            assert service.is_failure_injection_active("payment") is False
        assert "payment" not in service._failure_injections

    def test_get_config_returns_none_for_inactive(self, service):
        """Get config returns None for inactive
        비활성 주입에 대해 None을 반환하는지 확인.
        """
        assert service.get_failure_injection_config("payment") is None

    def test_get_config_returns_config_for_active(self, service):
        """Get config returns config for active
        활성 주입에 대해 설정을 반환하는지 확인.
        """
        config = {"enabled": True, "failure_rate": 0.5}
        service._failure_injections["payment"] = config
        assert service.get_failure_injection_config("payment") == config


# =============================================================================
# Singleton Tests
# =============================================================================


class TestSingleton:
    """get_control_api_service() 싱글톤 테스트."""

    def test_creates_singleton(self):
        """Creates singleton if not exists
        인스턴스가 없을 때 새로 생성하는지 확인.
        """
        from baldur.services.control_api_service import reset_control_api_service

        reset_control_api_service()
        try:
            result = get_control_api_service()
            assert result is not None
            assert isinstance(result, ControlAPIService)
        finally:
            reset_control_api_service()

    def test_returns_existing_singleton(self):
        """Returns existing singleton
        이미 존재하는 싱글톤을 반환하는지 확인.
        """
        from baldur.services.control_api_service import reset_control_api_service

        reset_control_api_service()
        try:
            svc1 = get_control_api_service()
            svc2 = get_control_api_service()
            assert svc1 is svc2
        finally:
            reset_control_api_service()


# =============================================================================
# get_metrics Tests
# =============================================================================


class TestGetMetrics:
    """get_metrics() 메서드 테스트."""

    @patch("baldur.factory.ProviderRegistry")
    @patch("baldur.services.metrics.updaters.update_retry_success_rates")
    @patch("baldur.services.metrics.updaters.update_dlq_pending_gauges")
    @patch("baldur.metrics.registry.get_registered_domains")
    def test_get_metrics_basic(
        self, mock_domains, mock_dlq, mock_retry, mock_registry, service
    ):
        """Get metrics returns expected structure
        get_metrics()가 올바른 구조를 반환하는지 확인.
        """
        mock_domains.return_value = ["payment", "point"]
        mock_dlq.return_value = {"payment": 3, "point": 1}
        mock_retry.return_value = {"payment": 95.0, "point": 100.0}

        # CB repository mock
        mock_cb_repo = MagicMock()
        mock_cb_repo.get_all_states.return_value = []
        mock_registry.circuit_breaker_repo.safe_get.return_value = mock_cb_repo

        # Failed op repository mock
        mock_failed_repo = MagicMock()
        mock_failed_repo.get_statistics.return_value = {
            "pending_count": 5,
            "total_count": 100,
            "avg_resolution_time_seconds": 30.0,
        }
        mock_registry.failed_op_repo.safe_get.return_value = mock_failed_repo

        result = service.get_metrics()

        assert "total_services" in result
        assert "healthy_services" in result
        assert "degraded_services" in result
        assert "total_dlq_pending" in result
        assert result["total_dlq_pending"] == 4  # 3 + 1
        assert "services" in result
        assert len(result["services"]) == 2
        assert "timestamp" in result
        assert "collection_duration_ms" in result

    @patch("baldur.factory.ProviderRegistry")
    @patch("baldur.services.metrics.updaters.update_retry_success_rates")
    @patch("baldur.services.metrics.updaters.update_dlq_pending_gauges")
    @patch("baldur.metrics.registry.get_registered_domains")
    def test_get_metrics_cb_repo_exception(
        self, mock_domains, mock_dlq, mock_retry, mock_registry, service
    ):
        """Get metrics handles CB repo exception
        CB 리포지토리 예외 시에도 정상적으로 반환하는지 확인.
        """
        mock_domains.return_value = ["payment"]
        mock_dlq.return_value = {"payment": 0}
        mock_retry.return_value = {"payment": 100.0}
        mock_registry.circuit_breaker_repo.safe_get.side_effect = Exception("DB down")
        mock_registry.failed_op_repo.safe_get.side_effect = Exception("DB down")

        result = service.get_metrics()
        assert result["healthy_services"] == 0
        assert result["degraded_services"] == 0

    @patch("baldur.factory.ProviderRegistry")
    @patch("baldur.services.metrics.updaters.update_retry_success_rates")
    @patch("baldur.services.metrics.updaters.update_dlq_pending_gauges")
    @patch("baldur.metrics.registry.get_registered_domains")
    def test_get_metrics_with_cb_states(
        self, mock_domains, mock_dlq, mock_retry, mock_registry, service
    ):
        """Get metrics with CB states
        CB 상태가 있을 때 healthy/degraded 카운트가 올바른지 확인.
        """
        mock_domains.return_value = ["payment", "point"]
        mock_dlq.return_value = {"payment": 0, "point": 0}
        mock_retry.return_value = {}

        # CB repository with states
        cb1 = MagicMock()
        cb1.service_name = "payment"
        cb1.state = "closed"
        cb2 = MagicMock()
        cb2.service_name = "point"
        cb2.state = "open"

        mock_cb_repo = MagicMock()
        mock_cb_repo.get_all_states.return_value = [cb1, cb2]
        mock_registry.circuit_breaker_repo.safe_get.return_value = mock_cb_repo
        mock_registry.failed_op_repo.safe_get.return_value = None

        result = service.get_metrics()
        assert result["healthy_services"] == 1
        assert result["degraded_services"] == 1


# =============================================================================
# execute validation path Tests (line 276 coverage)
# =============================================================================


class TestExecuteValidationPath:
    """execute()에서 validation 실패 경로 테스트."""

    def test_execute_validation_rejection(self, service):
        """Execute returns validation rejection
        _validate_request에서 거부되면 execute()가 바로 거부 응답을 반환하는지 확인.
        """
        req = _make_request(
            action=ControlAPIActions.INJECT_FAILURE,
            environment=ControlAPIEnvironments.OPS,
        )
        resp = service.execute(req)
        assert resp.status == "rejected"
        assert resp.error_code == "ACTION_FORBIDDEN_IN_ENVIRONMENT"


# =============================================================================
# ControlAPIService.__init__ fallback (doc 426, D1 — Null Object Pattern)
# =============================================================================


class TestControlAPIServiceInitFallbackBehavior:
    """ControlAPIService.__init__ gracefully falls back on import/init failure."""

    def test_importerror_assigns_null_cb_service(self):
        """ImportError on CB import → NullCircuitBreakerService assigned."""
        from baldur.services.control_api_service.service import (
            NullCircuitBreakerService,
        )

        with patch.dict(
            "sys.modules",
            {"baldur.services.circuit_breaker": None},
        ):
            service = ControlAPIService()
            assert isinstance(service.circuit_breaker, NullCircuitBreakerService)

    def test_exception_assigns_null_cb_service(self):
        """Runtime exception on CB init → NullCircuitBreakerService assigned."""
        from baldur.services.control_api_service.service import (
            NullCircuitBreakerService,
        )

        with patch(
            "baldur.services.circuit_breaker.get_circuit_breaker_service",
            side_effect=RuntimeError("Redis down"),
        ):
            service = ControlAPIService()
            assert isinstance(service.circuit_breaker, NullCircuitBreakerService)

    def test_importerror_assigns_none_replay_service(self):
        """ImportError on replay import → replay_service is None."""
        with patch.dict(
            "sys.modules",
            {
                "baldur.services.circuit_breaker": None,
                "baldur.services.replay_service": None,
            },
        ):
            service = ControlAPIService()
            assert service.replay_service is None

    def test_null_cb_service_operations_are_safe(self):
        """NullCircuitBreakerService operations return safe no-op values."""
        from baldur.services.control_api_service.service import (
            NullCircuitBreakerService,
        )

        null_cb = NullCircuitBreakerService()

        # force_close returns result with success=False
        result = null_cb.force_close("svc", reason="test")
        assert result.success is False
        assert result.service_name == "svc"

        # get_or_create_state returns null state
        state = null_cb.get_or_create_state("svc")
        assert state.failure_count == 0
        assert state.state == "closed"

        # get_all_states returns empty list
        assert null_cb.get_all_states() == []

        # record methods are no-ops (no exceptions)
        null_cb.record_failure("svc")
        null_cb.record_success("svc")
