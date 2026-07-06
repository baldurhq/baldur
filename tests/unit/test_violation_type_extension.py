"""
ViolationType 확장 및 Severity/ActionPolicy 매핑 테스트

순위 1, 2, 2.5, 3 구현 테스트:
- 신규 ViolationType 정의 (순위 1)
- Severity 매핑 (순위 2)
- ActionPolicy 매핑 (순위 2)
- CRITICAL → EventBus 연동 (순위 2.5)
"""

from unittest.mock import MagicMock, patch

from baldur.services.security import (
    ACTION_POLICY_BY_VIOLATION_TYPE,
    SEVERITY_BY_VIOLATION_TYPE,
    ActionPolicy,
    Severity,
    ViolationType,
)

# =============================================================================
# 순위 1: 신규 ViolationType 테스트
# =============================================================================


class TestNewViolationTypes:
    """신규 ViolationType 정의 테스트."""

    def test_corruption_shield_violation_types_exist(self):
        """
        Purpose:
            CorruptionShield / 이상 감지 관련 ViolationType이 정의되어 있는지 확인.
        """
        corruption_types = [
            "ANOMALY_STATISTICAL",
            "ANOMALY_BEHAVIORAL",
            "SCHEMA_VIOLATION",
            "BUSINESS_RULE_VIOLATION",
        ]

        for type_name in corruption_types:
            assert hasattr(ViolationType, type_name), (
                f"Missing ViolationType: {type_name}"
            )

    def test_audit_violation_types_exist(self):
        """
        Purpose:
            Audit 무결성 관련 ViolationType이 정의되어 있는지 확인.
        """
        audit_types = [
            "AUDIT_TAMPERING",
            "HASH_CHAIN_BROKEN",
            "WAL_CORRUPTION",
        ]

        for type_name in audit_types:
            assert hasattr(ViolationType, type_name), (
                f"Missing ViolationType: {type_name}"
            )

    def test_governance_violation_types_exist(self):
        """
        Purpose:
            Governance 위반 관련 ViolationType이 정의되어 있는지 확인.
        """
        governance_types = [
            "UNAUTHORIZED_OVERRIDE",
            "GOVERNANCE_BYPASS_ATTEMPT",
            "PRIVILEGE_ESCALATION",
        ]

        for type_name in governance_types:
            assert hasattr(ViolationType, type_name), (
                f"Missing ViolationType: {type_name}"
            )

    def test_all_new_violation_types_have_correct_values(self):
        """
        Purpose:
            신규 ViolationType의 값이 올바르게 설정되어 있는지 확인.
        """
        expected = {
            ViolationType.ANOMALY_STATISTICAL: "anomaly_statistical",
            ViolationType.ANOMALY_BEHAVIORAL: "anomaly_behavioral",
            ViolationType.SCHEMA_VIOLATION: "schema_violation",
            ViolationType.BUSINESS_RULE_VIOLATION: "business_rule_violation",
            ViolationType.AUDIT_TAMPERING: "audit_tampering",
            ViolationType.HASH_CHAIN_BROKEN: "hash_chain_broken",
            ViolationType.WAL_CORRUPTION: "wal_corruption",
            ViolationType.UNAUTHORIZED_OVERRIDE: "unauthorized_override",
            ViolationType.GOVERNANCE_BYPASS_ATTEMPT: "governance_bypass_attempt",
            ViolationType.PRIVILEGE_ESCALATION: "privilege_escalation",
        }

        for vtype, expected_value in expected.items():
            assert vtype.value == expected_value, (
                f"Wrong value for {vtype}: {vtype.value}"
            )


# =============================================================================
# 순위 2: Severity 매핑 테스트
# =============================================================================


class TestSeverityMapping:
    """Severity 매핑 테스트."""

    def test_all_violation_types_have_severity(self):
        """
        Purpose:
            모든 ViolationType에 Severity가 매핑되어 있는지 확인.
        """
        for vtype in ViolationType:
            assert vtype in SEVERITY_BY_VIOLATION_TYPE, f"Missing severity for: {vtype}"

    def test_audit_violations_are_critical(self):
        """
        Purpose:
            Audit 무결성 위반이 CRITICAL인지 확인.
        """
        critical_audit_types = [
            ViolationType.AUDIT_TAMPERING,
            ViolationType.HASH_CHAIN_BROKEN,
            ViolationType.WAL_CORRUPTION,
        ]

        for vtype in critical_audit_types:
            assert SEVERITY_BY_VIOLATION_TYPE[vtype] == Severity.CRITICAL, (
                f"{vtype} should be CRITICAL"
            )

    def test_governance_bypass_is_critical(self):
        """
        Purpose:
            Governance 우회 시도가 CRITICAL인지 확인.
        """
        assert (
            SEVERITY_BY_VIOLATION_TYPE[ViolationType.GOVERNANCE_BYPASS_ATTEMPT]
            == Severity.CRITICAL
        )
        assert (
            SEVERITY_BY_VIOLATION_TYPE[ViolationType.PRIVILEGE_ESCALATION]
            == Severity.CRITICAL
        )

    def test_anomaly_detection_is_high(self):
        """
        Purpose:
            이상 감지가 HIGH인지 확인.
        """
        high_types = [
            ViolationType.ANOMALY_STATISTICAL,
            ViolationType.ANOMALY_BEHAVIORAL,
            ViolationType.UNAUTHORIZED_OVERRIDE,
            ViolationType.BUSINESS_RULE_VIOLATION,
        ]

        for vtype in high_types:
            assert SEVERITY_BY_VIOLATION_TYPE[vtype] == Severity.HIGH, (
                f"{vtype} should be HIGH"
            )

    def test_schema_violation_is_medium(self):
        """
        Purpose:
            스키마 위반이 MEDIUM인지 확인.
        """
        assert (
            SEVERITY_BY_VIOLATION_TYPE[ViolationType.SCHEMA_VIOLATION]
            == Severity.MEDIUM
        )


# =============================================================================
# 순위 2: ActionPolicy 매핑 테스트
# =============================================================================


class TestActionPolicyMapping:
    """ActionPolicy 매핑 테스트."""

    def test_all_violation_types_have_policy(self):
        """
        Purpose:
            모든 ViolationType에 ActionPolicy가 매핑되어 있는지 확인.
        """
        for vtype in ViolationType:
            assert vtype in ACTION_POLICY_BY_VIOLATION_TYPE, (
                f"Missing policy for: {vtype}"
            )

    def test_privilege_escalation_triggers_emergency(self):
        """
        Purpose:
            PRIVILEGE_ESCALATION이 EMERGENCY_LEVEL_2를 포함하는지 확인.
        """
        policies = ACTION_POLICY_BY_VIOLATION_TYPE[ViolationType.PRIVILEGE_ESCALATION]
        assert ActionPolicy.EMERGENCY_LEVEL_2 in policies

    def test_audit_tampering_triggers_permanent_ban(self):
        """
        Purpose:
            AUDIT_TAMPERING이 IP_PERMANENT_BAN을 포함하는지 확인.
        """
        policies = ACTION_POLICY_BY_VIOLATION_TYPE[ViolationType.AUDIT_TAMPERING]
        assert ActionPolicy.IP_PERMANENT_BAN in policies

    def test_anomaly_behavioral_triggers_ip_ban(self):
        """
        Purpose:
            ANOMALY_BEHAVIORAL이 IP_TEMPORARY_BAN을 포함하는지 확인.
        """
        policies = ACTION_POLICY_BY_VIOLATION_TYPE[ViolationType.ANOMALY_BEHAVIORAL]
        assert ActionPolicy.IP_TEMPORARY_BAN in policies

    def test_schema_violation_triggers_block_and_log(self):
        """
        Purpose:
            SCHEMA_VIOLATION이 BLOCK_AND_LOG를 포함하는지 확인.
        """
        policies = ACTION_POLICY_BY_VIOLATION_TYPE[ViolationType.SCHEMA_VIOLATION]
        assert ActionPolicy.BLOCK_AND_LOG in policies


# =============================================================================
# 순위 2.5: EventBus 연동 테스트
# =============================================================================


class TestCriticalViolationEventBus:
    """CRITICAL 보안 위반 EventBus 연동 테스트."""

    def test_security_violation_event_types_exist(self):
        """
        Purpose:
            Security Violation EventType이 정의되어 있는지 확인.
        """
        from baldur.services.event_bus import EventType

        assert hasattr(EventType, "SECURITY_VIOLATION_DETECTED")
        assert hasattr(EventType, "SECURITY_VIOLATION_CRITICAL")

    def test_critical_violation_emits_event(self):
        """
        Purpose:
            CRITICAL 보안 위반 시 EventBus에 이벤트가 발행되는지 확인.
        """
        from baldur.services.security import SecurityViolationService

        with patch("baldur.services.event_bus.get_event_bus") as mock_get_bus:
            mock_bus = MagicMock()
            mock_get_bus.return_value = mock_bus

            service = SecurityViolationService()
            service._emit_critical_violation_event(
                violation_type="audit_tampering",
                incident_id=123,
                source_ip="1.2.3.4",
                user_id=456,
            )

            mock_bus.emit.assert_called_once()
            call_args = mock_bus.emit.call_args

            # EventType 확인
            from baldur.services.event_bus import EventType

            assert (
                call_args.kwargs["event_type"] == EventType.SECURITY_VIOLATION_CRITICAL
            )

            # 데이터 확인
            data = call_args.kwargs["data"]
            assert data["violation_type"] == "audit_tampering"
            assert data["severity"] == "critical"
            assert data["incident_id"] == 123

    def test_event_emission_failure_does_not_break_flow(self):
        """
        Purpose:
            EventBus 실패 시 예외가 발생하지 않는지 확인.
        """
        from baldur.services.security import SecurityViolationService

        with patch("baldur.services.event_bus.get_event_bus") as mock_get_bus:
            mock_get_bus.side_effect = Exception("EventBus unavailable")

            service = SecurityViolationService()
            # 예외 없이 실행되어야 함
            service._emit_critical_violation_event(
                violation_type="audit_tampering",
                incident_id=123,
                source_ip=None,
                user_id=None,
            )


# =============================================================================
# 통합 테스트
# =============================================================================


class TestViolationTypeIntegration:
    """ViolationType 통합 테스트."""

    def test_handle_violation_emits_event_for_critical(self):
        """
        Purpose:
            handle_violation이 CRITICAL 위반에 대해 이벤트를 발행하는지 확인.
        """
        from baldur.services.security import SecurityViolationService

        # Repository와 Cache mock
        mock_repo = MagicMock()
        mock_repo.create.return_value = MagicMock(id=999)

        mock_cache = MagicMock()
        mock_cache.get.return_value = None

        with patch.object(
            SecurityViolationService, "_emit_critical_violation_event"
        ) as mock_emit:
            service = SecurityViolationService(repository=mock_repo, cache=mock_cache)
            service.handle_violation(
                violation_type=ViolationType.AUDIT_TAMPERING,
                request_info={"ip": "1.2.3.4"},
                description="Test",
            )

            # CRITICAL이므로 이벤트 발행
            mock_emit.assert_called_once()

    def test_handle_violation_does_not_emit_for_medium(self):
        """
        Purpose:
            handle_violation이 MEDIUM 위반에 대해 이벤트를 발행하지 않는지 확인.
        """
        from baldur.services.security import SecurityViolationService

        mock_repo = MagicMock()
        mock_repo.create.return_value = MagicMock(id=999)

        mock_cache = MagicMock()
        mock_cache.get.return_value = None

        with patch.object(
            SecurityViolationService, "_emit_critical_violation_event"
        ) as mock_emit:
            service = SecurityViolationService(repository=mock_repo, cache=mock_cache)
            service.handle_violation(
                violation_type=ViolationType.SCHEMA_VIOLATION,  # MEDIUM
                request_info={"ip": "1.2.3.4"},
                description="Test",
            )

            # MEDIUM이므로 이벤트 발행 안함
            mock_emit.assert_not_called()
