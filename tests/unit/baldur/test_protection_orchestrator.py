"""
ProtectionOrchestrator 단위 테스트

순위 0, 0.3, 0.5 구현 테스트:
- ActionPolicy Enum 및 우선순위
- ProtectionOrchestrator 정책 실행
- 롤백 로직
- Baldur ViolationType 추가
"""

from unittest.mock import MagicMock, patch

import pytest

from baldur.services.security import (
    ACTION_POLICY_BY_VIOLATION_TYPE,
    ACTION_POLICY_PRIORITY,
    SEVERITY_BY_VIOLATION_TYPE,
    ActionPolicy,
    ProtectionOrchestrator,
    ProtectionResult,
    SecurityViolationService,
    Severity,
    ViolationType,
)

# =============================================================================
# ActionPolicy Enum Tests (순위 0)
# =============================================================================


class TestActionPolicyEnum:
    """ActionPolicy Enum 테스트."""

    def test_all_policies_defined(self):
        """
        Purpose:
            모든 ActionPolicy 값이 정의되어 있는지 확인.
        """
        expected_policies = [
            "EMERGENCY_LEVEL_3",
            "EMERGENCY_LEVEL_2",
            "EMERGENCY_LEVEL_1",
            "ACCOUNT_FREEZE",
            "SESSION_INVALIDATE",
            "IP_PERMANENT_BAN",
            "IP_TEMPORARY_BAN",
            "BLOCK_AND_LOG",
        ]

        for policy_name in expected_policies:
            assert hasattr(ActionPolicy, policy_name), (
                f"Missing ActionPolicy: {policy_name}"
            )

    def test_priority_mapping_complete(self):
        """
        Purpose:
            모든 ActionPolicy에 우선순위가 매핑되어 있는지 확인.
        """
        for policy in ActionPolicy:
            assert policy in ACTION_POLICY_PRIORITY, f"Missing priority for: {policy}"

    def test_priority_order(self):
        """
        Purpose:
            우선순위 순서가 올바른지 확인 (낮은 숫자 = 높은 우선순위).
        """
        # Emergency Level 3이 가장 높은 우선순위
        assert ACTION_POLICY_PRIORITY[ActionPolicy.EMERGENCY_LEVEL_3] == 1
        # Block and Log가 가장 낮은 우선순위
        assert ACTION_POLICY_PRIORITY[ActionPolicy.BLOCK_AND_LOG] == 8

        # Emergency > Account Freeze > Session > IP Ban > Block
        assert (
            ACTION_POLICY_PRIORITY[ActionPolicy.EMERGENCY_LEVEL_3]
            < ACTION_POLICY_PRIORITY[ActionPolicy.ACCOUNT_FREEZE]
            < ACTION_POLICY_PRIORITY[ActionPolicy.IP_PERMANENT_BAN]
            < ACTION_POLICY_PRIORITY[ActionPolicy.BLOCK_AND_LOG]
        )


class TestViolationTypePolicyMapping:
    """ViolationType → ActionPolicy 매핑 테스트."""

    def test_recovery_loop_triggers_emergency_3(self):
        """
        Purpose:
            RECOVERY_LOOP_DETECTED가 EMERGENCY_LEVEL_3을 트리거하는지 확인.
        """
        policies = ACTION_POLICY_BY_VIOLATION_TYPE.get(
            ViolationType.RECOVERY_LOOP_DETECTED, []
        )
        assert ActionPolicy.EMERGENCY_LEVEL_3 in policies

    def test_critical_violations_have_policies(self):
        """
        Purpose:
            CRITICAL 위반에 ActionPolicy가 매핑되어 있는지 확인.
        """
        critical_types = [
            vtype
            for vtype, sev in SEVERITY_BY_VIOLATION_TYPE.items()
            if sev == Severity.CRITICAL
        ]

        for vtype in critical_types:
            policies = ACTION_POLICY_BY_VIOLATION_TYPE.get(vtype, [])
            assert len(policies) > 0, f"No policy for CRITICAL violation: {vtype}"


# =============================================================================
# Baldur ViolationType Tests (순위 0.5)
# =============================================================================


class TestBaldurViolationTypes:
    """Baldur 루프 감지 관련 ViolationType 테스트."""

    def test_new_violation_types_exist(self):
        """
        Purpose:
            신규 ViolationType이 정의되어 있는지 확인.
        """
        new_types = [
            "RECOVERY_LOOP_DETECTED",
            "CONFLICTING_ADJUSTMENT",
            "HEALING_TIMEOUT",
            "FLAPPING_DETECTED",
        ]

        for type_name in new_types:
            assert hasattr(ViolationType, type_name), (
                f"Missing ViolationType: {type_name}"
            )

    def test_new_violation_types_have_severity(self):
        """
        Purpose:
            신규 ViolationType에 Severity가 매핑되어 있는지 확인.
        """
        new_types = [
            ViolationType.RECOVERY_LOOP_DETECTED,
            ViolationType.CONFLICTING_ADJUSTMENT,
            ViolationType.HEALING_TIMEOUT,
            ViolationType.FLAPPING_DETECTED,
        ]

        for vtype in new_types:
            assert vtype in SEVERITY_BY_VIOLATION_TYPE, f"Missing severity for: {vtype}"

    def test_recovery_loop_is_critical(self):
        """
        Purpose:
            RECOVERY_LOOP_DETECTED가 CRITICAL severity인지 확인.
        """
        assert (
            SEVERITY_BY_VIOLATION_TYPE[ViolationType.RECOVERY_LOOP_DETECTED]
            == Severity.CRITICAL
        )

    def test_flapping_is_high(self):
        """
        Purpose:
            FLAPPING_DETECTED가 HIGH severity인지 확인.
        """
        assert (
            SEVERITY_BY_VIOLATION_TYPE[ViolationType.FLAPPING_DETECTED] == Severity.HIGH
        )


# =============================================================================
# ProtectionResult Tests (순위 0)
# =============================================================================


class TestProtectionResult:
    """ProtectionResult dataclass 테스트."""

    def test_default_values(self):
        """
        Purpose:
            기본값이 올바르게 설정되는지 확인.
        """
        result = ProtectionResult(success=True)

        assert result.success is True
        assert result.executed_policies == []
        assert result.failed_policies == []
        assert result.highest_priority_succeeded is True
        assert result.rolled_back_policies == []
        assert result.rollback_success is True
        assert result.error_message == ""
        assert result.triggering_trace_id is None
        assert result.triggering_request_path is None

    def test_get_trace_url_with_template(self):
        """
        Purpose:
            trace_id가 있을 때 URL이 올바르게 생성되는지 확인.
        """
        result = ProtectionResult(
            success=True,
            triggering_trace_id="abc123",
        )

        url = result.get_trace_url("https://jaeger.example.com/trace/{trace_id}")
        assert url == "https://jaeger.example.com/trace/abc123"

    def test_get_trace_url_without_trace_id(self):
        """
        Purpose:
            trace_id가 없을 때 None을 반환하는지 확인.
        """
        result = ProtectionResult(success=True)
        assert (
            result.get_trace_url("https://jaeger.example.com/trace/{trace_id}") is None
        )


# =============================================================================
# ProtectionOrchestrator Tests (순위 0, 0.3)
# =============================================================================


class TestProtectionOrchestrator:
    """ProtectionOrchestrator 테스트."""

    @pytest.fixture
    def mock_service(self):
        """SecurityViolationService mock 생성."""
        service = MagicMock(spec=SecurityViolationService)
        service._invalidate_user_sessions = MagicMock()
        service._permanent_ip_ban = MagicMock()
        service._temporary_ip_ban = MagicMock()
        service._remove_ip_ban = MagicMock()
        return service

    @pytest.fixture
    def orchestrator(self, mock_service):
        """ProtectionOrchestrator 인스턴스 생성."""
        return ProtectionOrchestrator(mock_service)

    def test_empty_policies_returns_success(self, orchestrator):
        """
        Purpose:
            빈 정책 목록이 주어지면 성공을 반환하는지 확인.
        """
        result = orchestrator.execute_policies([], {})

        assert result.success is True
        assert result.executed_policies == []
        assert result.highest_priority_succeeded is True

    def test_highest_priority_executed_first(self, orchestrator):
        """
        Purpose:
            가장 높은 우선순위 정책이 먼저 실행되는지 확인.
        """
        policies = [
            ActionPolicy.BLOCK_AND_LOG,  # Priority 8
            ActionPolicy.IP_TEMPORARY_BAN,  # Priority 7
        ]

        result = orchestrator.execute_policies(
            policies,
            context={"source_ip": "1.2.3.4"},
        )

        # IP_TEMPORARY_BAN이 먼저 실행되어야 함
        assert result.executed_policies[0] == ActionPolicy.IP_TEMPORARY_BAN

    def test_block_and_log_executes(self, orchestrator):
        """
        Purpose:
            BLOCK_AND_LOG 정책이 올바르게 실행되는지 확인.
        """
        result = orchestrator.execute_policies(
            [ActionPolicy.BLOCK_AND_LOG],
            context={"reason": "test"},
        )

        assert result.success is True
        assert ActionPolicy.BLOCK_AND_LOG in result.executed_policies

    def test_session_invalidate_calls_service(self, orchestrator, mock_service):
        """
        Purpose:
            SESSION_INVALIDATE가 SecurityViolationService를 호출하는지 확인.
        """
        result = orchestrator.execute_policies(
            [ActionPolicy.SESSION_INVALIDATE],
            context={"user_id": 123},
        )

        assert result.success is True
        mock_service._invalidate_user_sessions.assert_called_once_with(123)

    def test_ip_ban_calls_service(self, orchestrator, mock_service):
        """
        Purpose:
            IP_TEMPORARY_BAN이 SecurityViolationService를 호출하는지 확인.
        """
        result = orchestrator.execute_policies(
            [ActionPolicy.IP_TEMPORARY_BAN],
            context={"source_ip": "1.2.3.4"},
        )

        assert result.success is True
        mock_service._temporary_ip_ban.assert_called_once_with("1.2.3.4")

    def test_trace_id_in_result(self, orchestrator):
        """
        Purpose:
            context의 trace_id가 결과에 포함되는지 확인.
        """
        result = orchestrator.execute_policies(
            [ActionPolicy.BLOCK_AND_LOG],
            context={"trace_id": "trace-abc", "request_path": "/api/payments/"},
        )

        assert result.triggering_trace_id == "trace-abc"
        assert result.triggering_request_path == "/api/payments/"


# =============================================================================
# Rollback Tests (순위 0.3)
# =============================================================================


class TestProtectionOrchestratorRollback:
    """ProtectionOrchestrator 롤백 로직 테스트."""

    @pytest.fixture
    def mock_service(self):
        """SecurityViolationService mock 생성."""
        service = MagicMock(spec=SecurityViolationService)
        service._invalidate_user_sessions = MagicMock()
        service._permanent_ip_ban = MagicMock()
        service._temporary_ip_ban = MagicMock()
        service._remove_ip_ban = MagicMock()
        return service

    @pytest.fixture
    def orchestrator(self, mock_service):
        """ProtectionOrchestrator 인스턴스 생성."""
        return ProtectionOrchestrator(mock_service)

    def test_highest_priority_failure_triggers_rollback(
        self, orchestrator, mock_service
    ):
        """
        Purpose:
            최고 우선순위 정책 실패 시 롤백이 트리거되는지 확인.
        """
        # IP_TEMPORARY_BAN을 실패하도록 설정
        mock_service._temporary_ip_ban.side_effect = Exception("Ban failed")

        result = orchestrator.execute_policies(
            [ActionPolicy.IP_TEMPORARY_BAN, ActionPolicy.BLOCK_AND_LOG],
            context={"source_ip": "1.2.3.4"},
        )

        # 최고 우선순위(IP_TEMPORARY_BAN)가 실패했으므로 전체 실패
        assert result.success is False
        assert result.highest_priority_succeeded is False
        assert ActionPolicy.IP_TEMPORARY_BAN in result.failed_policies

    def test_rollback_ip_ban(self, orchestrator, mock_service):
        """
        Purpose:
            IP 차단이 롤백되는지 확인.
        """
        # 롤백 테스트를 위해 직접 호출
        orchestrator._rollback_ip_ban({"source_ip": "1.2.3.4"})

        mock_service._remove_ip_ban.assert_called_once_with("1.2.3.4")

    def test_rollback_account_freeze(self, orchestrator):
        """
        Purpose:
            계정 동결 해제가 로그되는지 확인.
        """
        with patch("baldur.services.security.orchestrator.logger") as mock_logger:
            orchestrator._rollback_account_freeze({"user_id": 123})
            mock_logger.info.assert_called()


# =============================================================================
# Emergency Mode Integration Tests (순위 0)
# =============================================================================


class TestEmergencyModeIntegration:
    """Emergency Mode 통합 테스트."""

    @pytest.fixture
    def mock_service(self):
        """SecurityViolationService mock 생성."""
        return MagicMock(spec=SecurityViolationService)

    @pytest.fixture
    def orchestrator(self, mock_service):
        """ProtectionOrchestrator 인스턴스 생성."""
        return ProtectionOrchestrator(mock_service)

    def test_emergency_level_3_emits_event(self, orchestrator):
        """
        Purpose:
            EMERGENCY_LEVEL_3이 EventBus를 통해 이벤트를 발행하는지 확인.
        """
        with patch("baldur.services.event_bus.get_event_bus") as mock_get_bus:
            mock_bus = MagicMock()
            mock_get_bus.return_value = mock_bus

            orchestrator._execute_emergency_3(
                {
                    "reason": "Recovery loop",
                    "incident_id": 123,
                }
            )

            mock_bus.emit.assert_called_once()
            call_args = mock_bus.emit.call_args
            assert call_args.kwargs["data"]["level"] == 3

    def test_emergency_level_2_emits_event(self, orchestrator):
        """
        Purpose:
            EMERGENCY_LEVEL_2가 EventBus를 통해 이벤트를 발행하는지 확인.
        """
        with patch("baldur.services.event_bus.get_event_bus") as mock_get_bus:
            mock_bus = MagicMock()
            mock_get_bus.return_value = mock_bus

            orchestrator._execute_emergency_2(
                {
                    "reason": "Security violation",
                    "incident_id": 456,
                }
            )

            mock_bus.emit.assert_called_once()
            call_args = mock_bus.emit.call_args
            assert call_args.kwargs["data"]["level"] == 2

    def test_emergency_level_1_emits_event(self, orchestrator):
        """
        Purpose:
            EMERGENCY_LEVEL_1이 EventBus를 통해 이벤트를 발행하는지 확인.
        """
        with patch("baldur.services.event_bus.get_event_bus") as mock_get_bus:
            mock_bus = MagicMock()
            mock_get_bus.return_value = mock_bus

            orchestrator._execute_emergency_1(
                {
                    "reason": "Warning",
                }
            )

            mock_bus.emit.assert_called_once()
            call_args = mock_bus.emit.call_args
            assert call_args.kwargs["data"]["level"] == 1
