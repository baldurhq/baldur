"""보안, 리전 격리, Blast Radius 서비스의 감사 통합 테스트.

테스트 대상:
1. SecurityViolationService - 보안 위반 처리, IP 차단, 세션 무효화
2. RegionalIsolationGate - 리전 격리/해제
3. BlastRadiusService - 정책 설정, 의존성 추가, 서비스 격리/해제

검증 항목:
- log_security_violation_audit 호출 검증
- log_region_isolation_audit 호출 검증
- log_blast_radius_audit 호출 검증
- WAL 기반 무손실 감사 기록
"""

from unittest.mock import MagicMock, patch

import pytest

# =============================================================================
# Security Violation Audit Event Types Tests
# =============================================================================


class TestSecurityViolationAuditEventTypes:
    """보안 위반 AuditEventType 열거형 테스트."""

    def test_security_violation_event_type_exists(self):
        """Should have SECURITY_VIOLATION event type."""
        from baldur.audit.event_buffer import AuditEventType

        assert hasattr(AuditEventType, "SECURITY_VIOLATION")
        assert AuditEventType.SECURITY_VIOLATION.value == "security_violation"

    def test_security_ip_blocked_event_type_exists(self):
        """Should have SECURITY_IP_BLOCKED event type."""
        from baldur.audit.event_buffer import AuditEventType

        assert hasattr(AuditEventType, "SECURITY_IP_BLOCKED")
        assert AuditEventType.SECURITY_IP_BLOCKED.value == "security_ip_blocked"

    def test_security_session_invalidated_event_type_exists(self):
        """Should have SECURITY_SESSION_INVALIDATED event type."""
        from baldur.audit.event_buffer import AuditEventType

        assert hasattr(AuditEventType, "SECURITY_SESSION_INVALIDATED")
        assert (
            AuditEventType.SECURITY_SESSION_INVALIDATED.value
            == "security_session_invalidated"
        )

    def test_region_isolated_event_type_exists(self):
        """Should have REGION_ISOLATED event type."""
        from baldur.audit.event_buffer import AuditEventType

        assert hasattr(AuditEventType, "REGION_ISOLATED")
        assert AuditEventType.REGION_ISOLATED.value == "region_isolated"

    def test_region_restored_event_type_exists(self):
        """Should have REGION_RESTORED event type."""
        from baldur.audit.event_buffer import AuditEventType

        assert hasattr(AuditEventType, "REGION_RESTORED")
        assert AuditEventType.REGION_RESTORED.value == "region_restored"


# =============================================================================
# log_security_violation_audit Tests
# =============================================================================


class TestLogSecurityViolationAudit:
    """Tests for log_security_violation_audit function."""

    @pytest.fixture(autouse=True)
    def _require_pro(self):
        pytest.importorskip("baldur_pro")

    def test_returns_wal_sequence_on_success(self):
        """Should return WAL sequence number."""
        with patch(
            "baldur_pro.services.audit.compliance_audit._write_to_wal",
            return_value=42,
        ):
            from baldur_pro.services.audit import log_security_violation_audit

            result = log_security_violation_audit(
                violation_type="token_forged",
                action="handle_violation",
                target="ip:1.2.3.4",
                result="success",
                severity="critical",
            )

            assert result == 42

    def test_writes_to_wal_with_correct_event_type(self):
        """Should write to WAL with SECURITY_VIOLATION event type."""
        with patch(
            "baldur_pro.services.audit.compliance_audit._write_to_wal",
            return_value=1,
        ) as mock_wal:
            from baldur_pro.services.audit import log_security_violation_audit

            log_security_violation_audit(
                violation_type="injection_attempt",
                action="handle_violation",
                target="ip:10.0.0.1",
                result="success",
                severity="high",
                incident_id=123,
                source_ip="10.0.0.1",
            )

            mock_wal.assert_called_once()
            call_kwargs = mock_wal.call_args[1]
            assert call_kwargs["event_type"] == "SECURITY_VIOLATION"
            assert call_kwargs["source"] == "SecurityViolationService"
            assert call_kwargs["details"]["violation_type"] == "injection_attempt"
            assert call_kwargs["details"]["action"] == "handle_violation"
            assert call_kwargs["details"]["target"] == "ip:10.0.0.1"
            assert call_kwargs["details"]["result"] == "success"
            assert call_kwargs["details"]["incident_id"] == 123

    def test_block_ip_action_uses_correct_event_type(self):
        """Should use SECURITY_IP_BLOCKED event type for block_ip action."""
        with patch(
            "baldur_pro.services.audit.compliance_audit._write_to_wal",
            return_value=1,
        ) as mock_wal:
            from baldur_pro.services.audit import log_security_violation_audit

            log_security_violation_audit(
                violation_type="ip_ban_temporary",
                action="block_ip",
                target="ip:192.168.1.100",
                result="success",
                severity="high",
            )

            call_kwargs = mock_wal.call_args[1]
            assert call_kwargs["event_type"] == "SECURITY_IP_BLOCKED"

    def test_invalidate_session_action_uses_correct_event_type(self):
        """Should use SECURITY_SESSION_INVALIDATED event type for invalidate_session action."""
        with patch(
            "baldur_pro.services.audit.compliance_audit._write_to_wal",
            return_value=1,
        ) as mock_wal:
            from baldur_pro.services.audit import log_security_violation_audit

            log_security_violation_audit(
                violation_type="session_invalidation",
                action="invalidate_session",
                target="user:42",
                result="success",
                severity="high",
                user_id=42,
            )

            call_kwargs = mock_wal.call_args[1]
            assert call_kwargs["event_type"] == "SECURITY_SESSION_INVALIDATED"
            assert call_kwargs["details"]["user_id"] == 42

    def test_failed_result_sets_success_false(self):
        """Should set success=False when result is not 'success'."""
        with patch(
            "baldur_pro.services.audit.compliance_audit._write_to_wal",
            return_value=1,
        ) as mock_wal:
            from baldur_pro.services.audit import log_security_violation_audit

            log_security_violation_audit(
                violation_type="session_invalidation",
                action="invalidate_session",
                target="user:123",
                result="failed",
                severity="high",
            )

            call_kwargs = mock_wal.call_args[1]
            assert call_kwargs["success"] is False
            assert "failed" in call_kwargs["error_message"]


# =============================================================================
# log_region_isolation_audit Tests
# =============================================================================


class TestLogRegionIsolationAudit:
    """Tests for log_region_isolation_audit function."""

    @pytest.fixture(autouse=True)
    def _require_pro(self):
        pytest.importorskip("baldur_pro")

    def test_returns_wal_sequence_on_success(self):
        """Should return WAL sequence number."""
        with patch(
            "baldur_pro.services.audit.compliance_audit._write_to_wal",
            return_value=55,
        ):
            from baldur_pro.services.audit import log_region_isolation_audit

            result = log_region_isolation_audit(
                region="tokyo",
                action="isolate",
                result="success",
                reason="High error rate",
            )

            assert result == 55

    def test_isolate_action_uses_region_isolated_event_type(self):
        """Should use REGION_ISOLATED event type for isolate action."""
        with patch(
            "baldur_pro.services.audit.compliance_audit._write_to_wal",
            return_value=1,
        ) as mock_wal:
            from baldur_pro.services.audit import log_region_isolation_audit

            log_region_isolation_audit(
                region="seoul",
                action="isolate",
                result="success",
                reason="Network degradation",
                duration_seconds=300,
                operator="cluster-a",
            )

            call_kwargs = mock_wal.call_args[1]
            assert call_kwargs["event_type"] == "REGION_ISOLATED"
            assert call_kwargs["source"] == "RegionalIsolationGate"
            assert call_kwargs["details"]["region"] == "seoul"
            assert call_kwargs["details"]["action"] == "isolate"
            assert call_kwargs["details"]["duration_seconds"] == 300
            assert call_kwargs["target_id"] == "seoul"

    def test_restore_action_uses_region_restored_event_type(self):
        """Should use REGION_RESTORED event type for restore action."""
        with patch(
            "baldur_pro.services.audit.compliance_audit._write_to_wal",
            return_value=1,
        ) as mock_wal:
            from baldur_pro.services.audit import log_region_isolation_audit

            log_region_isolation_audit(
                region="tokyo",
                action="restore",
                result="success",
                reason="Manual restore",
                operator="ops-team",
            )

            call_kwargs = mock_wal.call_args[1]
            assert call_kwargs["event_type"] == "REGION_RESTORED"
            assert call_kwargs["details"]["action"] == "restore"

    def test_failed_isolation_sets_success_false(self):
        """Should set success=False for failed isolation."""
        with patch(
            "baldur_pro.services.audit.compliance_audit._write_to_wal",
            return_value=1,
        ) as mock_wal:
            from baldur_pro.services.audit import log_region_isolation_audit

            log_region_isolation_audit(
                region="osaka",
                action="isolate",
                result="failed",
                reason="Redis not available",
            )

            call_kwargs = mock_wal.call_args[1]
            assert call_kwargs["success"] is False


# =============================================================================
# SecurityViolationService Audit Integration Tests
# =============================================================================


class TestSecurityViolationServiceAuditIntegration:
    """Tests for SecurityViolationService audit integration."""

    @pytest.fixture
    def mock_repository(self):
        """Create mock repository."""
        mock_repo = MagicMock()
        mock_incident = MagicMock()
        mock_incident.id = 999
        mock_repo.create.return_value = mock_incident
        return mock_repo

    @pytest.fixture
    def mock_cache(self):
        """Create mock cache."""
        mock_cache = MagicMock()
        mock_cache.get.return_value = None
        return mock_cache

    def test_handle_violation_calls_audit(self, mock_repository, mock_cache):
        """Should call log_security_violation_audit on handle_violation."""
        with patch(
            "baldur.services.security.service.log_security_violation_audit"
        ) as mock_audit:
            from baldur.services.security.service import SecurityViolationService
            from baldur.services.security.types import ViolationType

            service = SecurityViolationService(
                repository=mock_repository,
                cache=mock_cache,
            )

            service.handle_violation(
                violation_type=ViolationType.SIGNATURE_INVALID,
                request_info={"ip": "1.2.3.4"},
                description="Test violation",
            )

            mock_audit.assert_called_once()
            call_kwargs = mock_audit.call_args[1]
            assert call_kwargs["violation_type"] == "signature_invalid"
            assert call_kwargs["action"] == "handle_violation"
            assert call_kwargs["target"] == "ip:1.2.3.4"
            assert call_kwargs["result"] == "success"
            assert call_kwargs["incident_id"] == 999

    def test_temporary_ip_ban_calls_audit(self, mock_repository, mock_cache):
        """Should call log_security_violation_audit on _temporary_ip_ban."""
        with patch(
            "baldur.services.security.service.log_security_violation_audit"
        ) as mock_audit:
            from baldur.services.security.service import SecurityViolationService

            service = SecurityViolationService(
                repository=mock_repository,
                cache=mock_cache,
            )

            service._temporary_ip_ban("10.0.0.1", hours=2)

            mock_audit.assert_called_once()
            call_kwargs = mock_audit.call_args[1]
            assert call_kwargs["violation_type"] == "ip_ban_temporary"
            assert call_kwargs["action"] == "block_ip"
            assert call_kwargs["target"] == "ip:10.0.0.1"
            assert call_kwargs["details"]["ban_type"] == "temporary"
            assert call_kwargs["details"]["duration_hours"] == 2

    def test_permanent_ip_ban_calls_audit(self, mock_repository, mock_cache):
        """Should call log_security_violation_audit on _permanent_ip_ban."""
        with patch(
            "baldur.services.security.service.log_security_violation_audit"
        ) as mock_audit:
            from baldur.services.security.service import SecurityViolationService

            service = SecurityViolationService(
                repository=mock_repository,
                cache=mock_cache,
            )

            service._permanent_ip_ban("192.168.1.100")

            mock_audit.assert_called_once()
            call_kwargs = mock_audit.call_args[1]
            assert call_kwargs["violation_type"] == "ip_ban_permanent"
            assert call_kwargs["action"] == "block_ip"
            assert call_kwargs["severity"] == "critical"
            assert call_kwargs["details"]["ban_type"] == "permanent"

    def test_invalidate_user_sessions_calls_audit(self, mock_repository, mock_cache):
        """Should call log_security_violation_audit on _invalidate_user_sessions."""
        with patch(
            "baldur.services.security.service.log_security_violation_audit"
        ) as mock_audit:
            from baldur.services.security.service import SecurityViolationService

            service = SecurityViolationService(
                repository=mock_repository,
                cache=mock_cache,
            )

            # Django DB 세션 삭제의 DB 연결 시도를 방지 (테스트 대상이 아님)
            with patch.object(
                SecurityViolationService,
                "_invalidate_django_db_sessions",
                return_value=[],
            ):
                service._invalidate_user_sessions(user_id=42)

            mock_audit.assert_called_once()
            call_kwargs = mock_audit.call_args[1]
            assert call_kwargs["violation_type"] == "session_invalidation"
            assert call_kwargs["action"] == "invalidate_session"
            assert call_kwargs["target"] == "user:42"
            assert call_kwargs["user_id"] == 42


# =============================================================================
# RegionalIsolationGate Audit Integration Tests
# =============================================================================


class TestRegionalIsolationGateAuditIntegration:
    """Tests for RegionalIsolationGate audit integration."""

    @pytest.fixture
    def mock_redis(self):
        """Create mock Redis client."""
        mock_redis = MagicMock()
        mock_redis.set.return_value = True
        mock_redis.delete.return_value = 1
        mock_redis.sadd.return_value = 1
        mock_redis.srem.return_value = 1
        mock_redis.get.return_value = None
        return mock_redis

    @pytest.fixture
    def mock_identity(self):
        """Create mock cluster identity."""
        mock_id = MagicMock()
        mock_id.cluster_id = "test-cluster"
        mock_id.region = "test-region"
        return mock_id

    def test_isolate_region_calls_audit_on_success(self, mock_redis, mock_identity):
        """Should call log_region_isolation_audit on successful isolation."""
        with patch(
            "baldur.services.isolation.regional_gate.log_region_isolation_audit"
        ) as mock_audit:
            from baldur.services.isolation.regional_gate import (
                RegionalIsolationGate,
            )

            gate = RegionalIsolationGate(
                global_redis=mock_redis,
                cluster_identity=mock_identity,
            )
            gate._initialized = True

            result = gate.isolate_region(
                "tokyo", reason="High error rate", duration_seconds=300
            )

            assert result is True
            mock_audit.assert_called_once()
            call_kwargs = mock_audit.call_args[1]
            assert call_kwargs["region"] == "tokyo"
            assert call_kwargs["action"] == "isolate"
            assert call_kwargs["result"] == "success"
            assert call_kwargs["reason"] == "High error rate"
            assert call_kwargs["duration_seconds"] == 300
            assert call_kwargs["operator"] == "test-cluster"

    def test_isolate_region_calls_audit_on_failure(self, mock_identity):
        """Should call log_region_isolation_audit on failed isolation."""
        mock_redis = MagicMock()
        mock_redis.set.side_effect = Exception("Redis connection failed")

        with patch(
            "baldur.services.isolation.regional_gate.log_region_isolation_audit"
        ) as mock_audit:
            from baldur.services.isolation.regional_gate import (
                RegionalIsolationGate,
            )

            gate = RegionalIsolationGate(
                global_redis=mock_redis,
                cluster_identity=mock_identity,
            )
            gate._initialized = True

            result = gate.isolate_region("osaka", reason="Test", duration_seconds=60)

            assert result is False
            mock_audit.assert_called_once()
            call_kwargs = mock_audit.call_args[1]
            assert call_kwargs["region"] == "osaka"
            assert call_kwargs["action"] == "isolate"
            assert call_kwargs["result"] == "failed"
            assert "error" in call_kwargs["details"]

    def test_restore_region_calls_audit_on_success(self, mock_redis, mock_identity):
        """Should call log_region_isolation_audit on successful restore."""
        with patch(
            "baldur.services.isolation.regional_gate.log_region_isolation_audit"
        ) as mock_audit:
            from baldur.services.isolation.regional_gate import (
                RegionalIsolationGate,
            )

            gate = RegionalIsolationGate(
                global_redis=mock_redis,
                cluster_identity=mock_identity,
            )
            gate._initialized = True

            result = gate.restore_region("seoul")

            assert result is True
            mock_audit.assert_called_once()
            call_kwargs = mock_audit.call_args[1]
            assert call_kwargs["region"] == "seoul"
            assert call_kwargs["action"] == "restore"
            assert call_kwargs["result"] == "success"


# =============================================================================
# BlastRadiusService Audit Integration Tests
# =============================================================================


class TestBlastRadiusServiceAuditIntegration:
    """Tests for BlastRadiusService audit integration."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        """Reset BlastRadiusService singleton before each test."""
        from baldur.services.blast_radius.service import BlastRadiusService

        BlastRadiusService._instance = None
        yield
        BlastRadiusService._instance = None

    def test_set_policy_calls_audit(self):
        """Should call log_blast_radius_audit on set_policy."""
        with patch(
            "baldur.services.blast_radius.service.log_blast_radius_audit"
        ) as mock_audit:
            from baldur.services.blast_radius.models import BlastRadiusLevel
            from baldur.services.blast_radius.service import BlastRadiusService

            service = BlastRadiusService()
            service.clear()

            service.set_policy(
                service_name="production",
                level=BlastRadiusLevel.CONTAINED,
                max_affected_percentage=15.0,
                auto_isolate=True,
            )

            mock_audit.assert_called_once()
            call_kwargs = mock_audit.call_args[1]
            assert call_kwargs["target_service"] == "production"
            assert call_kwargs["action"] == "set_policy"
            assert call_kwargs["blast_radius"] == "contained"
            assert call_kwargs["traffic_percent"] == 15.0

    def test_add_dependency_calls_audit(self):
        """Should call log_blast_radius_audit on add_dependency."""
        with patch(
            "baldur.services.blast_radius.service.log_blast_radius_audit"
        ) as mock_audit:
            from baldur.services.blast_radius.service import BlastRadiusService

            service = BlastRadiusService()
            service.clear()

            service.add_dependency(
                source_service="payment",
                target_service="order",
                dependency_type="sync",
                criticality="high",
            )

            mock_audit.assert_called_once()
            call_kwargs = mock_audit.call_args[1]
            assert call_kwargs["action"] == "add_dependency"
            assert call_kwargs["target_service"] == "order"
            assert call_kwargs["target_domain"] == "payment"
            assert call_kwargs["blast_radius"] == "high"

    def test_isolate_service_calls_audit(self):
        """Should call log_blast_radius_audit on isolate_service."""
        with patch(
            "baldur.services.blast_radius.service.log_blast_radius_audit"
        ) as mock_audit:
            from baldur.services.blast_radius.service import BlastRadiusService

            service = BlastRadiusService()
            service.clear()

            result = service.isolate_service("payment-gateway")

            assert result is True
            mock_audit.assert_called_once()
            call_kwargs = mock_audit.call_args[1]
            assert call_kwargs["action"] == "isolate_service"
            assert call_kwargs["target_service"] == "payment-gateway"
            assert call_kwargs["blast_radius"] == "manual"

    def test_release_isolation_calls_audit(self):
        """Should call log_blast_radius_audit on release_isolation."""
        with patch(
            "baldur.services.blast_radius.service.log_blast_radius_audit"
        ) as mock_audit:
            from baldur.services.blast_radius.service import BlastRadiusService

            service = BlastRadiusService()
            service.clear()

            # First isolate, then release
            service._isolated_services.add("order-service")

            result = service.release_isolation("order-service")

            assert result is True
            mock_audit.assert_called_once()
            call_kwargs = mock_audit.call_args[1]
            assert call_kwargs["action"] == "release_isolation"
            assert call_kwargs["target_service"] == "order-service"
            assert call_kwargs["blast_radius"] == "released"

    def test_isolate_already_isolated_service_does_not_call_audit(self):
        """Should not call audit when isolating already isolated service."""
        with patch(
            "baldur.services.blast_radius.service.log_blast_radius_audit"
        ) as mock_audit:
            from baldur.services.blast_radius.service import BlastRadiusService

            service = BlastRadiusService()
            service.clear()

            # Already isolated
            service._isolated_services.add("payment")

            result = service.isolate_service("payment")

            assert result is False
            mock_audit.assert_not_called()

    def test_auto_isolate_calls_audit(self):
        """Should call log_blast_radius_audit for each auto-isolated service."""
        with patch(
            "baldur.services.blast_radius.service.log_blast_radius_audit"
        ) as mock_audit:
            from baldur.services.blast_radius.service import BlastRadiusService

            service = BlastRadiusService()
            service.clear()

            service._auto_isolate(["svc-a", "svc-b"])

            assert mock_audit.call_count == 2

            # Check first service
            first_call_kwargs = mock_audit.call_args_list[0][1]
            assert first_call_kwargs["action"] == "auto_isolate"
            assert first_call_kwargs["target_service"] == "svc-a"

            # Check second service
            second_call_kwargs = mock_audit.call_args_list[1][1]
            assert second_call_kwargs["target_service"] == "svc-b"


# =============================================================================
# Audit Helpers Export Tests
# =============================================================================


class TestAuditHelpersExport:
    """Tests for audit helpers module exports."""

    @pytest.fixture(autouse=True)
    def _require_pro(self):
        pytest.importorskip("baldur_pro")

    def test_log_security_violation_audit_is_exported(self):
        """Should export log_security_violation_audit from baldur_pro.services.audit."""
        from baldur_pro.services.audit import log_security_violation_audit

        assert callable(log_security_violation_audit)

    def test_log_region_isolation_audit_is_exported(self):
        """Should export log_region_isolation_audit from baldur_pro.services.audit."""
        from baldur_pro.services.audit import log_region_isolation_audit

        assert callable(log_region_isolation_audit)

    def test_exports_are_in_all(self):
        """Should include new audit functions in __all__."""
        from baldur_pro.services import audit

        assert "log_security_violation_audit" in audit.__all__
        assert "log_region_isolation_audit" in audit.__all__
