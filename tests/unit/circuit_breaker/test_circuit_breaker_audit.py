"""
Circuit Breaker Audit 통합 테스트.

모든 상태 변경에서 audit 기록이 정상적으로 호출되는지 검증.

실행:
    pytest packages/baldur-python/tests/unit/test_circuit_breaker_audit.py -v
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import Mock, patch

import pytest

# Every test patches baldur_pro.services.audit.log_cb_state_change_audit;
# CB audit is entirely PRO-backed, so skip the whole module PRO-absent.
pytest.importorskip("baldur_pro")

pytestmark = pytest.mark.requires_pro


class TestCircuitBreakerManualControlAudit:
    """수동 제어 시 audit 기록 테스트."""

    @pytest.fixture
    def mock_repository(self):
        """Mock CircuitBreakerStateRepository."""
        repo = Mock()
        return repo

    @pytest.fixture
    def mock_config(self):
        """Mock CircuitBreakerConfig."""
        config = Mock()
        config.enabled = True
        config.manual_override_ttl_minutes = 90
        config.recovery_timeout = 60
        config.success_threshold = 2
        return config

    @pytest.fixture
    def service(self, mock_repository, mock_config):
        """CircuitBreakerService with mocked dependencies."""
        from baldur.services.circuit_breaker.service import CircuitBreakerService

        svc = CircuitBreakerService(config=mock_config, repository=mock_repository)
        return svc

    # =========================================================================
    # force_open 테스트
    # =========================================================================

    @patch(
        "baldur.services.circuit_breaker.manual_control._is_system_enabled",
        return_value=True,
    )
    @patch("baldur_pro.services.audit.log_cb_state_change_audit")
    def test_force_open_calls_audit(
        self, mock_audit, mock_system, service, mock_repository
    ):
        """force_open 성공 시 audit 기록이 호출되어야 함."""
        # Setup
        mock_repository.atomic_force_open.return_value = (True, "closed", "open")

        # Execute - actor info is now read from ActorContext (SYSTEM_ACTOR fallback)
        result = service.force_open(
            service_name="test_service",
            reason="테스트 차단",
        )

        # Assert
        assert result.success is True
        mock_audit.assert_called_once()
        call_args = mock_audit.call_args
        assert call_args.kwargs["cb_name"] == "test_service"
        assert call_args.kwargs["old_state"] == "closed"
        assert call_args.kwargs["new_state"] == "open"
        assert "force_open" in call_args.kwargs["reason"]
        # actor_id and actor_type are now passed from ActorContext
        assert "actor_id" in call_args.kwargs
        assert "actor_type" in call_args.kwargs

    @patch(
        "baldur.services.circuit_breaker.manual_control._is_system_enabled",
        return_value=True,
    )
    @patch("baldur_pro.services.audit.log_cb_state_change_audit")
    def test_force_open_already_open_no_audit(
        self, mock_audit, mock_system, service, mock_repository
    ):
        """force_open 시 이미 open 상태면 audit 호출 안함."""
        # Setup - 이미 open 상태
        mock_repository.atomic_force_open.return_value = (True, "open", "open")

        # Execute
        result = service.force_open(service_name="test_service")

        # Assert
        assert result.success is True
        mock_audit.assert_not_called()

    # =========================================================================
    # force_close 테스트
    # =========================================================================

    @patch(
        "baldur.services.circuit_breaker.manual_control._is_system_enabled",
        return_value=True,
    )
    @patch("baldur_pro.services.audit.log_cb_state_change_audit")
    def test_force_close_calls_audit(
        self, mock_audit, mock_system, service, mock_repository
    ):
        """force_close 성공 시 audit 기록이 호출되어야 함."""
        # Setup
        mock_repository.atomic_force_close.return_value = (True, "open", "closed")

        # Execute - actor info is now read from ActorContext (SYSTEM_ACTOR fallback)
        result = service.force_close(
            service_name="test_service",
            reason="복구 확인",
        )

        # Assert
        assert result.success is True
        mock_audit.assert_called_once()
        call_args = mock_audit.call_args
        assert call_args.kwargs["cb_name"] == "test_service"
        assert call_args.kwargs["old_state"] == "open"
        assert call_args.kwargs["new_state"] == "closed"
        assert "force_close" in call_args.kwargs["reason"]

    @patch(
        "baldur.services.circuit_breaker.manual_control._is_system_enabled",
        return_value=True,
    )
    @patch("baldur_pro.services.audit.log_cb_state_change_audit")
    def test_force_close_already_closed_no_audit(
        self, mock_audit, mock_system, service, mock_repository
    ):
        """force_close 시 이미 closed 상태면 audit 호출 안함."""
        # Setup
        mock_repository.atomic_force_close.return_value = (True, "closed", "closed")

        # Execute
        result = service.force_close(service_name="test_service")

        # Assert
        assert result.success is True
        mock_audit.assert_not_called()

    # =========================================================================
    # reset 테스트
    # =========================================================================

    @patch("baldur_pro.services.audit.log_cb_state_change_audit")
    def test_reset_calls_audit(self, mock_audit, service, mock_repository):
        """reset 성공 시 audit 기록이 호출되어야 함."""
        # Setup
        mock_repository.atomic_reset.return_value = (True, "open", "closed")

        # Execute
        result = service.reset(
            service_name="test_service",
            reason="상태 초기화",
            controlled_by=1,
        )

        # Assert
        assert result.success is True
        mock_audit.assert_called_once()
        call_args = mock_audit.call_args
        assert call_args.kwargs["cb_name"] == "test_service"
        assert call_args.kwargs["old_state"] == "open"
        assert call_args.kwargs["new_state"] == "closed"
        assert "reset" in call_args.kwargs["reason"]

    @patch("baldur_pro.services.audit.log_cb_state_change_audit")
    def test_reset_same_state_no_audit(self, mock_audit, service, mock_repository):
        """reset 시 상태 변경 없으면 audit 호출 안함."""
        # Setup
        mock_repository.atomic_reset.return_value = (True, "closed", "closed")

        # Execute
        result = service.reset(service_name="test_service")

        # Assert
        assert result.success is True
        mock_audit.assert_not_called()


class TestCircuitBreakerAutoRecoveryAudit:
    """자동 복구 시 audit 기록 테스트."""

    @pytest.fixture
    def mock_repository(self):
        """Mock CircuitBreakerStateRepository."""
        repo = Mock()
        return repo

    @pytest.fixture
    def mock_config(self):
        """Mock CircuitBreakerConfig."""
        config = Mock()
        config.enabled = True
        config.recovery_timeout = 60  # 60초
        config.success_threshold = 2
        config.failure_threshold = 5
        return config

    @pytest.fixture
    def service(self, mock_repository, mock_config):
        """CircuitBreakerService with mocked dependencies."""
        from baldur.services.circuit_breaker.service import CircuitBreakerService

        svc = CircuitBreakerService(config=mock_config, repository=mock_repository)
        return svc

    # =========================================================================
    # should_allow: OPEN → HALF_OPEN 전환 테스트
    # =========================================================================

    @patch("baldur_pro.services.audit.log_cb_state_change_audit")
    def test_should_allow_open_to_half_open_calls_audit(
        self, mock_audit, service, mock_repository
    ):
        """recovery_timeout 경과 후 OPEN → HALF_OPEN 전환 시 audit 호출."""
        from baldur.services.circuit_breaker.config import CircuitState

        # Setup - OPEN 상태, recovery_timeout 경과
        state = Mock()
        state.state = CircuitState.OPEN
        state.opened_at = datetime.now(UTC) - timedelta(seconds=120)  # 120초 전
        mock_repository.get_or_create.return_value = state
        # 476: repository owns the OPEN→HALF_OPEN atomic transition.
        mock_repository.try_acquire_half_open_slot.return_value = (
            True,
            CircuitState.OPEN.value,
            CircuitState.HALF_OPEN.value,
        )

        # Execute
        result = service.should_allow("test_service")

        # Assert
        assert result is True
        mock_repository.try_acquire_half_open_slot.assert_called_once()
        mock_audit.assert_called_once()
        call_args = mock_audit.call_args
        assert call_args.kwargs["cb_name"] == "test_service"
        assert (
            "OPEN" in str(call_args.kwargs["old_state"])
            or call_args.kwargs["old_state"] == CircuitState.OPEN
        )
        assert (
            "HALF_OPEN" in str(call_args.kwargs["new_state"])
            or call_args.kwargs["new_state"] == CircuitState.HALF_OPEN
        )
        assert "auto_recovery" in call_args.kwargs["reason"]

    @patch("baldur_pro.services.audit.log_cb_state_change_audit")
    def test_should_allow_open_not_expired_no_audit(
        self, mock_audit, service, mock_repository
    ):
        """recovery_timeout 미경과 시 audit 호출 안함."""
        from baldur.services.circuit_breaker.config import CircuitState

        # Setup - OPEN 상태, 아직 timeout 미경과
        state = Mock()
        state.state = CircuitState.OPEN
        state.opened_at = datetime.now(UTC) - timedelta(seconds=30)  # 30초 전
        mock_repository.get_or_create.return_value = state

        # Execute
        result = service.should_allow("test_service")

        # Assert
        assert result is False
        mock_audit.assert_not_called()

    # =========================================================================
    # record_success: HALF_OPEN → CLOSED 전환 테스트
    # =========================================================================

    @patch("baldur_pro.services.audit.log_cb_state_change_audit")
    def test_record_success_half_open_to_closed_calls_audit(
        self, mock_audit, service, mock_repository, mock_config
    ):
        """HALF_OPEN에서 success_threshold 도달 시 CLOSED 전환 및 audit 호출."""
        from baldur.interfaces.repositories import CircuitBreakerCloseAttempt

        # Setup - HALF_OPEN 상태
        state = Mock()
        state.state = "half_open"
        state.manually_controlled = False
        mock_repository.get_or_create.return_value = state

        # 497 D1/D2: HALF_OPEN branch uses record_success_with_close_check.
        # threshold 도달 → did_close=True
        closed_state = Mock()
        closed_state.state = "closed"
        closed_state.success_count = 0
        mock_repository.record_success_with_close_check.return_value = (
            CircuitBreakerCloseAttempt(state=closed_state, did_close=True)
        )
        mock_config.success_threshold = 2

        # Execute
        service.record_success("test_service")

        # Assert
        mock_repository.record_success_with_close_check.assert_called_once_with(
            "test_service", 2
        )
        mock_audit.assert_called_once()
        call_args = mock_audit.call_args
        assert call_args.kwargs["cb_name"] == "test_service"
        assert call_args.kwargs["old_state"] == "half_open"
        assert call_args.kwargs["new_state"] == "closed"
        assert "auto_recovery" in call_args.kwargs["reason"]

    @patch("baldur_pro.services.audit.log_cb_state_change_audit")
    def test_record_success_not_enough_no_audit(
        self, mock_audit, service, mock_repository, mock_config
    ):
        """HALF_OPEN에서 success_threshold 미달 시 audit 호출 안함."""
        from baldur.interfaces.repositories import CircuitBreakerCloseAttempt

        # Setup - HALF_OPEN 상태
        state = Mock()
        state.state = "half_open"
        state.manually_controlled = False
        mock_repository.get_or_create.return_value = state

        # 497 D1/D2: threshold 미달 → did_close=False, audit not called.
        still_half_open = Mock()
        still_half_open.state = "half_open"
        still_half_open.success_count = 1
        mock_repository.record_success_with_close_check.return_value = (
            CircuitBreakerCloseAttempt(state=still_half_open, did_close=False)
        )
        mock_config.success_threshold = 2

        # Execute
        service.record_success("test_service")

        # Assert
        mock_repository.update_state.assert_not_called()
        mock_audit.assert_not_called()


class TestCircuitBreakerAuditFailSafe:
    """Audit 실패 시 비즈니스 로직 영향 없음 테스트."""

    @pytest.fixture
    def mock_repository(self):
        """Mock CircuitBreakerStateRepository."""
        repo = Mock()
        return repo

    @pytest.fixture
    def mock_config(self):
        """Mock CircuitBreakerConfig."""
        config = Mock()
        config.enabled = True
        config.manual_override_ttl_minutes = 90
        return config

    @pytest.fixture
    def service(self, mock_repository, mock_config):
        """CircuitBreakerService with mocked dependencies."""
        from baldur.services.circuit_breaker.service import CircuitBreakerService

        svc = CircuitBreakerService(config=mock_config, repository=mock_repository)
        return svc

    @patch(
        "baldur.services.circuit_breaker.manual_control._is_system_enabled",
        return_value=True,
    )
    @patch("baldur_pro.services.audit.log_cb_state_change_audit")
    def test_audit_failure_does_not_affect_force_open(
        self, mock_audit, mock_system, service, mock_repository
    ):
        """Audit 실패해도 force_open은 정상 동작."""
        # Setup
        mock_repository.atomic_force_open.return_value = (True, "closed", "open")
        mock_audit.side_effect = Exception("Audit failed!")

        # Execute
        result = service.force_open(service_name="test_service")

        # Assert - Audit 예외에도 불구하고 성공
        assert result.success is True
        assert result.new_state == "open"

    @patch(
        "baldur.services.circuit_breaker.manual_control._is_system_enabled",
        return_value=True,
    )
    @patch("baldur_pro.services.audit.log_cb_state_change_audit")
    def test_audit_failure_does_not_affect_force_close(
        self, mock_audit, mock_system, service, mock_repository
    ):
        """Audit 실패해도 force_close는 정상 동작."""
        # Setup
        mock_repository.atomic_force_close.return_value = (True, "open", "closed")
        mock_audit.side_effect = Exception("Audit failed!")

        # Execute
        result = service.force_close(service_name="test_service")

        # Assert - Audit 예외에도 불구하고 성공
        assert result.success is True
        assert result.new_state == "closed"


class TestCircuitBreakerAuditContent:
    """Audit 기록 내용 검증 테스트."""

    @pytest.fixture
    def mock_repository(self):
        """Mock CircuitBreakerStateRepository."""
        repo = Mock()
        return repo

    @pytest.fixture
    def mock_config(self):
        """Mock CircuitBreakerConfig."""
        config = Mock()
        config.enabled = True
        config.manual_override_ttl_minutes = 90
        return config

    @pytest.fixture
    def service(self, mock_repository, mock_config):
        """CircuitBreakerService with mocked dependencies."""
        from baldur.services.circuit_breaker.service import CircuitBreakerService

        svc = CircuitBreakerService(config=mock_config, repository=mock_repository)
        return svc

    @patch(
        "baldur.services.circuit_breaker.manual_control._is_system_enabled",
        return_value=True,
    )
    @patch("baldur_pro.services.audit.log_cb_state_change_audit")
    def test_force_open_audit_includes_reason(
        self, mock_audit, mock_system, service, mock_repository
    ):
        """force_open audit에 reason이 포함되어야 함."""
        mock_repository.atomic_force_open.return_value = (True, "closed", "open")

        service.force_open(service_name="payment", reason="PG 장애")

        call_args = mock_audit.call_args
        assert "PG 장애" in call_args.kwargs["reason"]
        assert "force_open" in call_args.kwargs["reason"]

    @patch(
        "baldur.services.circuit_breaker.manual_control._is_system_enabled",
        return_value=True,
    )
    @patch("baldur_pro.services.audit.log_cb_state_change_audit")
    def test_force_open_audit_default_reason(
        self, mock_audit, mock_system, service, mock_repository
    ):
        """force_open reason이 없으면 기본값 사용."""
        mock_repository.atomic_force_open.return_value = (True, "closed", "open")

        service.force_open(service_name="payment")

        call_args = mock_audit.call_args
        assert "force_open: manual" in call_args.kwargs["reason"]


# =============================================================================
# Auto-Open Audit 통합 테스트 (레거시 log_config_change → audit_helpers 마이그레이션)
# =============================================================================


class TestCircuitBreakerAutoOpenAudit:
    """
    CB 자동 OPEN 시 audit_helpers.log_cb_state_change_audit 사용 검증.

    기존 문제:
    - _log_circuit_open_audit()이 baldur.audit.log_config_change 직접 호출
    - WAL 기반 누락 0 보장 및 해시 체인 연결 누락

    수정 후:
    - audit_helpers.log_cb_state_change_audit() 사용
    - WAL 기록 + 해시 체인 연결 보장

    Ref: 20_AUDIT_UNIFICATION_PLAN.md
    """

    @pytest.fixture
    def mock_repository(self):
        """Mock CircuitBreakerStateRepository."""
        repo = Mock()
        repo.get_state.return_value = None  # No existing state
        repo.update_state.return_value = True
        return repo

    @pytest.fixture
    def mock_config(self):
        """Mock CircuitBreakerConfig with threshold settings."""
        config = Mock()
        config.enabled = True
        config.failure_threshold = 5
        config.recovery_timeout = 60
        config.half_open_max_calls = 3
        config.cb_open_burn_rate_multiplier = 2.0
        config.notification_cooldown_minutes = 5
        config.success_threshold = 2
        return config

    @pytest.fixture
    def service(self, mock_repository, mock_config):
        """CircuitBreakerService with mocked dependencies."""
        from baldur.services.circuit_breaker.service import CircuitBreakerService

        svc = CircuitBreakerService(config=mock_config, repository=mock_repository)
        return svc

    @patch("baldur_pro.services.audit.log_cb_state_change_audit")
    def test_auto_open_uses_audit_helpers(self, mock_audit, service):
        """
        자동 OPEN 시 audit_helpers.log_cb_state_change_audit 호출 확인.

        레거시 log_config_change 대신 audit_helpers 사용 검증.
        """
        snapshot = {
            "failure_count": 5,
            "threshold": 5,
            "last_failures": ["timeout", "connection_error"],
        }

        # Execute - 내부 메서드 직접 호출
        service._log_circuit_open_audit("payment_service", snapshot)

        # Assert - audit_helpers 호출 확인
        mock_audit.assert_called_once()
        call_args = mock_audit.call_args

        # 파라미터 검증
        assert call_args.kwargs["cb_name"] == "payment_service"
        assert call_args.kwargs["old_state"] == "closed"
        assert call_args.kwargs["new_state"] == "open"
        assert "auto_trigger" in call_args.kwargs["reason"]
        assert "failures=5" in call_args.kwargs["reason"]

    @patch("baldur_pro.services.audit.log_cb_state_change_audit")
    def test_auto_open_audit_includes_threshold_info(self, mock_audit, service):
        """auto-open reason에 threshold 정보가 포함되어야 함."""
        snapshot = {
            "failure_count": 10,
            "threshold": 10,
        }

        service._log_circuit_open_audit("order_service", snapshot)

        call_args = mock_audit.call_args
        reason = call_args.kwargs["reason"]

        assert "threshold=10" in reason
        assert "failures=10" in reason

    @patch("baldur_pro.services.audit.log_cb_state_change_audit")
    def test_auto_open_audit_handles_missing_snapshot_fields(self, mock_audit, service):
        """snapshot에 필드가 없어도 에러 없이 처리."""
        empty_snapshot = {}

        # Should not raise
        service._log_circuit_open_audit("test_service", empty_snapshot)

        mock_audit.assert_called_once()
        call_args = mock_audit.call_args

        # N/A로 대체되어야 함
        assert "N/A" in call_args.kwargs["reason"]

    @patch("baldur_pro.services.audit.log_cb_state_change_audit")
    def test_auto_open_audit_exception_handling(self, mock_audit, service):
        """audit 실패해도 CB 동작에는 영향 없어야 함."""
        mock_audit.side_effect = Exception("Audit system unavailable")

        # Should not raise - graceful degradation
        service._log_circuit_open_audit("payment", {"failure_count": 5})

        # Verify audit was attempted
        mock_audit.assert_called_once()

    @patch(
        "baldur_pro.services.audit.log_cb_state_change_audit", side_effect=ImportError
    )
    def test_auto_open_audit_import_error_handling(self, mock_audit, service):
        """audit_helpers import 실패 시에도 에러 없이 처리."""
        # Should not raise
        service._log_circuit_open_audit("payment", {"failure_count": 5})

    @patch("baldur_pro.services.audit.log_cb_state_change_audit")
    def test_auto_open_audit_not_using_legacy_log_config_change(
        self, mock_audit, service
    ):
        """
        레거시 log_config_change가 아닌 audit_helpers 사용 확인.

        이 테스트는 마이그레이션이 올바르게 되었는지 검증합니다.
        """
        with patch("baldur.audit.log_config_change") as legacy_mock:
            service._log_circuit_open_audit("payment", {"failure_count": 5})

            # 레거시 호출 없어야 함
            legacy_mock.assert_not_called()

            # 새 audit_helpers 호출되어야 함
            mock_audit.assert_called_once()
