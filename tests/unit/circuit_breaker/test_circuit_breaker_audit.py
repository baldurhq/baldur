"""
Circuit Breaker audit integration tests.

Verifies that the audit record is written on every state change.

Run:
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
    """Audit recording on manual control."""

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
    # force_open
    # =========================================================================

    @patch(
        "baldur.services.circuit_breaker.manual_control._is_system_enabled",
        return_value=True,
    )
    @patch("baldur_pro.services.audit.log_cb_state_change_audit")
    def test_force_open_calls_audit(
        self, mock_audit, mock_system, service, mock_repository
    ):
        """A successful force_open writes an audit record."""
        # Setup
        mock_repository.atomic_force_open.return_value = (True, "closed", "open")

        # Execute - actor info is now read from ActorContext (SYSTEM_ACTOR fallback)
        result = service.force_open(
            service_name="test_service",
            reason="test block",
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
        """force_open on an already-open circuit writes no audit record."""
        # Setup - already open
        mock_repository.atomic_force_open.return_value = (True, "open", "open")

        # Execute
        result = service.force_open(service_name="test_service")

        # Assert
        assert result.success is True
        mock_audit.assert_not_called()

    # =========================================================================
    # force_close
    # =========================================================================

    @patch(
        "baldur.services.circuit_breaker.manual_control._is_system_enabled",
        return_value=True,
    )
    @patch("baldur_pro.services.audit.log_cb_state_change_audit")
    def test_force_close_calls_audit(
        self, mock_audit, mock_system, service, mock_repository
    ):
        """A successful force_close writes an audit record."""
        # Setup
        mock_repository.atomic_force_close.return_value = (True, "open", "closed")

        # Execute - actor info is now read from ActorContext (SYSTEM_ACTOR fallback)
        result = service.force_close(
            service_name="test_service",
            reason="recovery confirmed",
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
        """force_close on an already-closed circuit writes no audit record."""
        # Setup
        mock_repository.atomic_force_close.return_value = (True, "closed", "closed")

        # Execute
        result = service.force_close(service_name="test_service")

        # Assert
        assert result.success is True
        mock_audit.assert_not_called()

    # =========================================================================
    # reset
    # =========================================================================

    @patch("baldur_pro.services.audit.log_cb_state_change_audit")
    def test_reset_calls_audit(self, mock_audit, service, mock_repository):
        """A successful reset writes an audit record."""
        # Setup
        mock_repository.atomic_reset.return_value = (True, "open", "closed")

        # Execute
        result = service.reset(
            service_name="test_service",
            reason="state reset",
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
        """A reset that changes no state writes no audit record."""
        # Setup
        mock_repository.atomic_reset.return_value = (True, "closed", "closed")

        # Execute
        result = service.reset(service_name="test_service")

        # Assert
        assert result.success is True
        mock_audit.assert_not_called()


class TestCircuitBreakerAutoRecoveryAudit:
    """Audit recording on automatic recovery."""

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
        config.recovery_timeout = 60  # seconds
        config.success_threshold = 2
        config.failure_threshold = 5
        config.sliding_window_size = 100
        return config

    @pytest.fixture
    def service(self, mock_repository, mock_config):
        """CircuitBreakerService with mocked dependencies."""
        from baldur.services.circuit_breaker.service import CircuitBreakerService

        svc = CircuitBreakerService(config=mock_config, repository=mock_repository)
        return svc

    # =========================================================================
    # should_allow: OPEN -> HALF_OPEN transition
    # =========================================================================

    @patch("baldur_pro.services.audit.log_cb_state_change_audit")
    def test_should_allow_open_to_half_open_calls_audit(
        self, mock_audit, service, mock_repository
    ):
        """The OPEN -> HALF_OPEN transition after recovery_timeout writes an audit record."""
        from baldur.services.circuit_breaker.config import CircuitState

        # Setup - OPEN, recovery_timeout elapsed
        state = Mock()
        state.state = CircuitState.OPEN
        state.opened_at = datetime.now(UTC) - timedelta(seconds=120)  # 120 s ago
        # Automatic OPEN, not an operator block — a manual pin takes a
        # different admission branch entirely.
        state.manually_controlled = False
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
        """No audit record while recovery_timeout has not elapsed."""
        from baldur.services.circuit_breaker.config import CircuitState

        # Setup - OPEN, timeout not yet elapsed
        state = Mock()
        state.state = CircuitState.OPEN
        state.opened_at = datetime.now(UTC) - timedelta(seconds=30)  # 30 s ago
        state.manually_controlled = False
        mock_repository.get_or_create.return_value = state

        # Execute
        result = service.should_allow("test_service")

        # Assert
        assert result is False
        mock_audit.assert_not_called()

    # =========================================================================
    # record_success: HALF_OPEN -> CLOSED transition
    # =========================================================================

    @patch("baldur_pro.services.audit.log_cb_state_change_audit")
    def test_record_success_half_open_to_closed_calls_audit(
        self, mock_audit, service, mock_repository, mock_config
    ):
        """Reaching success_threshold in HALF_OPEN closes the circuit and writes an audit record."""
        from baldur.interfaces.repositories import CircuitBreakerCloseAttempt

        # Setup - HALF_OPEN
        state = Mock()
        state.state = "half_open"
        state.manually_controlled = False
        mock_repository.get_or_create.return_value = state

        # 497 D1/D2: HALF_OPEN branch uses record_success_with_close_check.
        # threshold reached -> did_close=True
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
        """Below success_threshold in HALF_OPEN no audit record is written."""
        from baldur.interfaces.repositories import CircuitBreakerCloseAttempt

        # Setup - HALF_OPEN
        state = Mock()
        state.state = "half_open"
        state.manually_controlled = False
        mock_repository.get_or_create.return_value = state

        # 497 D1/D2: threshold not reached -> did_close=False, audit not called.
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
    """An audit failure never affects the business logic."""

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
        """force_open still works when the audit write fails."""
        # Setup
        mock_repository.atomic_force_open.return_value = (True, "closed", "open")
        mock_audit.side_effect = Exception("Audit failed!")

        # Execute
        result = service.force_open(service_name="test_service")

        # Assert - succeeds despite the audit exception
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
        """force_close still works when the audit write fails."""
        # Setup
        mock_repository.atomic_force_close.return_value = (True, "open", "closed")
        mock_audit.side_effect = Exception("Audit failed!")

        # Execute
        result = service.force_close(service_name="test_service")

        # Assert - succeeds despite the audit exception
        assert result.success is True
        assert result.new_state == "closed"


class TestCircuitBreakerAuditContent:
    """Audit record content."""

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
        """The force_open audit record carries the reason."""
        mock_repository.atomic_force_open.return_value = (True, "closed", "open")

        service.force_open(service_name="payment", reason="PG outage")

        call_args = mock_audit.call_args
        assert "PG outage" in call_args.kwargs["reason"]
        assert "force_open" in call_args.kwargs["reason"]

    @patch(
        "baldur.services.circuit_breaker.manual_control._is_system_enabled",
        return_value=True,
    )
    @patch("baldur_pro.services.audit.log_cb_state_change_audit")
    def test_force_open_audit_default_reason(
        self, mock_audit, mock_system, service, mock_repository
    ):
        """A force_open without a reason uses the default."""
        mock_repository.atomic_force_open.return_value = (True, "closed", "open")

        service.force_open(service_name="payment")

        call_args = mock_audit.call_args
        assert "force_open: manual" in call_args.kwargs["reason"]


# =============================================================================
# Auto-open audit integration (legacy log_config_change -> audit_helpers migration)
# =============================================================================


class TestCircuitBreakerAutoOpenAudit:
    """
    An automatic OPEN uses audit_helpers.log_cb_state_change_audit.

    Before:
    - _log_circuit_open_audit() called baldur.audit.log_config_change directly
    - no WAL-backed zero-loss guarantee, no hash-chain link

    After:
    - audit_helpers.log_cb_state_change_audit() is used
    - WAL record + hash-chain link guaranteed

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
        An automatic OPEN calls audit_helpers.log_cb_state_change_audit.

        Verifies audit_helpers is used instead of the legacy log_config_change.
        """
        snapshot = {
            "failure_count": 5,
            "threshold": 5,
            "last_failures": ["timeout", "connection_error"],
        }

        # Execute - call the internal method directly
        service._log_circuit_open_audit("payment_service", snapshot)

        # Assert - audit_helpers was called
        mock_audit.assert_called_once()
        call_args = mock_audit.call_args

        # Parameter checks
        assert call_args.kwargs["cb_name"] == "payment_service"
        assert call_args.kwargs["old_state"] == "closed"
        assert call_args.kwargs["new_state"] == "open"
        assert "auto_trigger" in call_args.kwargs["reason"]
        assert "failures=5" in call_args.kwargs["reason"]

    @patch("baldur_pro.services.audit.log_cb_state_change_audit")
    def test_auto_open_audit_includes_threshold_info(self, mock_audit, service):
        """The auto-open reason carries the threshold."""
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
        """A snapshot missing fields is handled without error."""
        empty_snapshot = {}

        # Should not raise
        service._log_circuit_open_audit("test_service", empty_snapshot)

        mock_audit.assert_called_once()
        call_args = mock_audit.call_args

        # substituted with N/A
        assert "N/A" in call_args.kwargs["reason"]

    @patch("baldur_pro.services.audit.log_cb_state_change_audit")
    def test_auto_open_audit_exception_handling(self, mock_audit, service):
        """An audit failure must not affect the breaker."""
        mock_audit.side_effect = Exception("Audit system unavailable")

        # Should not raise - graceful degradation
        service._log_circuit_open_audit("payment", {"failure_count": 5})

        # Verify audit was attempted
        mock_audit.assert_called_once()

    @patch(
        "baldur_pro.services.audit.log_cb_state_change_audit", side_effect=ImportError
    )
    def test_auto_open_audit_import_error_handling(self, mock_audit, service):
        """An audit_helpers import failure is handled without error."""
        # Should not raise
        service._log_circuit_open_audit("payment", {"failure_count": 5})

    @patch("baldur_pro.services.audit.log_cb_state_change_audit")
    def test_auto_open_audit_not_using_legacy_log_config_change(
        self, mock_audit, service
    ):
        """
        audit_helpers is used, not the legacy log_config_change.

        Verifies the migration landed correctly.
        """
        with patch("baldur.audit.log_config_change") as legacy_mock:
            service._log_circuit_open_audit("payment", {"failure_count": 5})

            # no legacy call
            legacy_mock.assert_not_called()

            # the new audit_helpers call
            mock_audit.assert_called_once()
