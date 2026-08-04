"""
Tests for services/control_api_service.py - Control API Service.
Unit tests for the Control API's core business logic: blocking/allowing a
service, failure injection, and risk assessment.

Covers:
- ReasonClassification enum
- classify_reason()
- assess_risk_level()
- the ControlRequest / ControlResponse dataclasses
- ControlAPIService.execute() routing
- _validate_request() (inject forbidden in ops, override TTL rules)
- _execute_allow/block/override/reset/inject_failure/inject_success()
- _gather_evidence(), _record_audit()
- get_status(), get_service_status()
- is_failure_injection_active(), get_failure_injection_config()
- the get_control_api_service() singleton
"""

from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from baldur.adapters.memory.circuit_breaker import (
    InMemoryCircuitBreakerStateRepository,
)
from baldur.core.constants import (
    ControlAPIActions,
    ControlAPIEnvironments,
    RiskLevels,
)
from baldur.services.circuit_breaker.config import CircuitBreakerConfig
from baldur.services.circuit_breaker.service import CircuitBreakerService
from baldur.services.control_api_service import (
    ControlAPIService,
    ControlRequest,
    ControlResponse,
    ReasonClassification,
    assess_risk_level,
    classify_reason,
    get_control_api_service,
)
from baldur.services.replay_service import ReplayService
from baldur.settings.circuit_breaker import MAX_MANUAL_OVERRIDE_TTL_MINUTES
from baldur.utils.time import utc_now

_TTL_PINNING_ACTIONS = (
    ControlAPIActions.ALLOW,
    ControlAPIActions.BLOCK,
    ControlAPIActions.OVERRIDE,
)

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def mock_cb_service():
    """CircuitBreakerService mock."""
    cb = MagicMock()
    # force_close / force_open results
    success_result = MagicMock()
    success_result.success = True
    success_result.error = None
    # The expiry the manual override actually stored. Left as a bare MagicMock
    # it would make every "effective_until is not None" assertion pass on the
    # mock's own truthiness, whatever the response path did with the value.
    success_result.expires_at = utc_now() + timedelta(minutes=90)
    cb.force_close.return_value = success_result
    cb.force_open.return_value = success_result
    cb.reset.return_value = success_result

    # get_or_create_state result
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
    """A ControlAPIService with its dependencies mocked."""
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
    """ControlRequest factory."""
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
    """The ReasonClassification enum."""

    def test_enum_values(self):
        """Enum values
        Every ReasonClassification member carries its declared string value.
        """
        assert (
            ReasonClassification.EXTERNAL_DEPENDENCY_FAILURE
            == "external-dependency-failure"
        )
        assert ReasonClassification.CHAOS_EXPERIMENT == "chaos-experiment"
        assert ReasonClassification.UNKNOWN == "unknown"

    def test_enum_is_string(self):
        """Enum is str subclass
        The enum values are plain strings.
        """
        assert isinstance(ReasonClassification.MAINTENANCE_WINDOW.value, str)


# =============================================================================
# classify_reason Tests
# =============================================================================


class TestClassifyReason:
    """classify_reason()."""

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
        Each reason string maps to its expected classification.
        """
        assert classify_reason(reason) == expected

    def test_case_insensitive(self):
        """Case insensitive classification
        Classification ignores case.
        """
        assert (
            classify_reason("SCHEDULED MAINTENANCE")
            == ReasonClassification.MAINTENANCE_WINDOW.value
        )

    def test_no_matching_pattern(self):
        """No matching pattern returns manual_intervention
        An unmatched reason falls back to 'manual-intervention'.
        """
        assert (
            classify_reason("Some random reason")
            == ReasonClassification.MANUAL_INTERVENTION.value
        )

    def test_empty_reason(self):
        """Empty reason string
        An empty reason falls back to 'manual-intervention'.
        """
        assert classify_reason("") == ReasonClassification.MANUAL_INTERVENTION.value


# =============================================================================
# assess_risk_level Tests
# =============================================================================


class TestAssessRiskLevel:
    """assess_risk_level()."""

    def test_allow_in_test(self):
        """Allow in test environment
        allow in the test environment is INFO.
        """
        assert (
            assess_risk_level(ControlAPIActions.ALLOW, ControlAPIEnvironments.TEST)
            == RiskLevels.INFO
        )

    def test_block_in_ops(self):
        """Block in ops environment
        block in ops is HIGH.
        """
        assert (
            assess_risk_level(ControlAPIActions.BLOCK, ControlAPIEnvironments.OPS)
            == RiskLevels.HIGH
        )

    def test_override_in_ops(self):
        """Override in ops environment
        override in ops is CRITICAL.
        """
        assert (
            assess_risk_level(ControlAPIActions.OVERRIDE, ControlAPIEnvironments.OPS)
            == RiskLevels.CRITICAL
        )

    def test_inject_failure_in_ops(self):
        """Inject failure in ops environment
        inject_failure in ops is FORBIDDEN.
        """
        assert (
            assess_risk_level(
                ControlAPIActions.INJECT_FAILURE, ControlAPIEnvironments.OPS
            )
            == RiskLevels.FORBIDDEN
        )

    def test_inject_success_in_chaos(self):
        """Inject success in chaos
        inject_success in chaos is INFO.
        """
        assert (
            assess_risk_level(
                ControlAPIActions.INJECT_SUCCESS, ControlAPIEnvironments.CHAOS
            )
            == RiskLevels.INFO
        )

    def test_unknown_combination_defaults_warning(self):
        """Unknown combination defaults to WARNING
        An undefined combination falls back to WARNING.
        """
        assert assess_risk_level("unknown_action", "unknown_env") == RiskLevels.WARNING


# =============================================================================
# ControlRequest / ControlResponse Tests
# =============================================================================


class TestControlRequest:
    """The ControlRequest dataclass."""

    def test_default_values(self):
        """Default values
        The declared defaults are applied.
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
        assert req.request_id  # auto-generated UUID

    def test_custom_values(self):
        """Custom values
        Explicit values override the defaults.
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
    """The ControlResponse dataclass."""

    def test_to_dict_minimal(self):
        """Minimal to_dict
        to_dict() with only the required fields populated.
        """
        resp = ControlResponse(status="success", action_applied="allow")
        d = resp.to_dict()
        assert d["status"] == "success"
        assert d["action_applied"] == "allow"
        assert "correlation_id" in d
        # Empty optional fields are dropped
        assert "system_state" not in d
        assert "error_code" not in d

    def test_to_dict_full(self):
        """Full to_dict
        to_dict() with every field populated.
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
    """ControlAPIService.execute() action routing."""

    def test_execute_allow(self, service):
        """Execute allow action
        The allow action runs.
        """
        req = _make_request(action=ControlAPIActions.ALLOW)
        resp = service.execute(req)
        assert resp.status == "success"
        assert resp.action_applied == "allow"
        assert resp.system_state == "allow"

    def test_execute_block(self, service):
        """Execute block action
        The block action runs.
        """
        req = _make_request(action=ControlAPIActions.BLOCK)
        resp = service.execute(req)
        assert resp.status == "success"
        assert resp.action_applied == "block"

    def test_execute_override(self, service):
        """Execute override action
        The override action runs.
        """
        req = _make_request(action=ControlAPIActions.OVERRIDE, ttl_minutes=30)
        resp = service.execute(req)
        assert resp.status == "success"
        assert resp.action_applied == "override"

    def test_execute_reset(self, service):
        """Execute reset action
        The reset action runs.
        """
        req = _make_request(action=ControlAPIActions.RESET)
        resp = service.execute(req)
        assert resp.status == "success"
        assert resp.action_applied == "reset"

    def test_execute_inject_failure(self, service):
        """Execute inject_failure action
        The inject_failure action runs.
        """
        req = _make_request(action=ControlAPIActions.INJECT_FAILURE)
        resp = service.execute(req)
        assert resp.status == "success"
        assert resp.action_applied == "inject_failure"

    def test_execute_inject_success(self, service):
        """Execute inject_success action
        The inject_success action runs.
        """
        req = _make_request(action=ControlAPIActions.INJECT_SUCCESS)
        resp = service.execute(req)
        assert resp.status == "success"
        assert resp.action_applied == "inject_success"

    def test_execute_unknown_action(self, service):
        """Execute unknown action
        An unknown action returns an error response.
        """
        req = _make_request(action="unknown_action")
        resp = service.execute(req)
        assert resp.status == "error"
        assert resp.error_code == "UNKNOWN_ACTION"

    def test_execute_exception_handling(self, service, mock_cb_service):
        """Execute with exception
        An exception during execution returns an error response.
        """
        mock_cb_service.force_close.side_effect = RuntimeError("Unexpected error")
        req = _make_request(action=ControlAPIActions.ALLOW)
        resp = service.execute(req)
        assert resp.status == "error"
        assert resp.error_code == "EXECUTION_ERROR"

    def test_execute_adds_metadata(self, service):
        """Execute adds classification and risk
        reason_classification and risk_level are attached after execution.
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
    """_validate_request()."""

    def test_inject_failure_forbidden_in_ops(self, service):
        """Inject failure forbidden in ops
        inject_failure is rejected in ops.
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
        inject_success is rejected in ops.
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
        An override in ops requires a TTL.
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
        An override TTL above 60 minutes is rejected in ops.
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
        A TTL of 60 minutes or less in ops passes validation.
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
        allow passes validation in ops.
        """
        req = _make_request(
            action=ControlAPIActions.ALLOW,
            environment=ControlAPIEnvironments.OPS,
        )
        result = service._validate_request(req)
        assert result is None

    def test_inject_failure_in_chaos_valid(self, service):
        """Inject failure in chaos is valid
        inject_failure passes validation in chaos.
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
    """_execute_allow()."""

    def test_allow_calls_force_close(self, service, mock_cb_service):
        """Allow calls force_close
        The allow action calls circuit_breaker.force_close().
        """
        req = _make_request(action=ControlAPIActions.ALLOW)
        service._execute_allow(req)
        mock_cb_service.force_close.assert_called_once()

    def test_allow_failure(self, service, mock_cb_service):
        """Allow failure response
        A failed force_close returns an error response.
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
    """_execute_block()."""

    def test_block_calls_force_open(self, service, mock_cb_service):
        """Block calls force_open
        The block action calls circuit_breaker.force_open().
        """
        req = _make_request(action=ControlAPIActions.BLOCK)
        service._execute_block(req)
        mock_cb_service.force_open.assert_called_once()

    def test_block_forwards_the_requested_ttl_to_the_circuit_breaker(
        self, service, mock_cb_service
    ):
        """The typed lifetime reaches force_open instead of being recomputed."""
        req = _make_request(action=ControlAPIActions.BLOCK, ttl_minutes=30)

        service._execute_block(req)

        assert mock_cb_service.force_open.call_args.kwargs["ttl_minutes"] == 30

    def test_block_reports_the_expiry_the_circuit_breaker_stored(
        self, service, mock_cb_service
    ):
        """effective_until mirrors the stored expiry, typed TTL or not.

        The response used to recompute it — including a 90-minute literal on
        the ops branch — so it could promise a lift time the breaker did not
        have.
        """
        req = _make_request(
            action=ControlAPIActions.BLOCK,
            environment=ControlAPIEnvironments.OPS,
            ttl_minutes=None,
        )

        resp = service._execute_block(req)

        stored = mock_cb_service.force_open.return_value.expires_at
        assert resp.effective_until == stored.isoformat()

    def test_block_omits_effective_until_when_no_expiry_was_read_back(
        self, service, mock_cb_service
    ):
        """A degraded read-back must not be papered over with a computed value."""
        mock_cb_service.force_open.return_value.expires_at = None
        req = _make_request(action=ControlAPIActions.BLOCK, ttl_minutes=30)

        resp = service._execute_block(req)

        assert resp.status == "success"
        assert resp.effective_until is None

    def test_block_failure(self, service, mock_cb_service):
        """Block failure response
        A failed force_open returns an error response.
        """
        fail_result = MagicMock()
        fail_result.success = False
        fail_result.error = "CB Error"
        mock_cb_service.force_open.return_value = fail_result

        req = _make_request(action=ControlAPIActions.BLOCK)
        resp = service._execute_block(req)
        assert resp.status == "error"


class TestExecuteOverride:
    """_execute_override()."""

    def test_override_success(self, service, mock_cb_service):
        """Override success
        A successful override returns the expected response.
        """
        req = _make_request(action=ControlAPIActions.OVERRIDE, ttl_minutes=15)
        resp = service._execute_override(req)
        assert resp.status == "success"
        assert resp.action_applied == "override"
        assert resp.system_state == "allow"
        stored = mock_cb_service.force_close.return_value.expires_at
        assert resp.effective_until == stored.isoformat()
        assert mock_cb_service.force_close.call_args.kwargs["ttl_minutes"] == 15

    def test_override_failure(self, service, mock_cb_service):
        """Override failure
        A failed override returns an error response.
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
    """_execute_reset()."""

    def test_reset_success(self, service, mock_cb_service):
        """Reset success
        A successful reset returns the expected response.
        """
        req = _make_request(action=ControlAPIActions.RESET)
        resp = service._execute_reset(req)
        assert resp.status == "success"
        assert resp.action_applied == "reset"
        assert resp.system_state == "allow"

    def test_reset_calls_reset_and_never_force_close(self, service, mock_cb_service):
        """Reset must reset.

        This called ``reset_to_default()``, a name carried only by
        ``NullCircuitBreakerService``, so every real service raised
        AttributeError and the handler "fell back" to ``force_close`` — pinning
        a manual override instead of clearing one. The retired test asserted
        that fallback, which is why a mock kept it green: the mock answered to
        a method the real service never had.
        """
        req = _make_request(action=ControlAPIActions.RESET)
        resp = service._execute_reset(req)

        assert resp.status == "success"
        mock_cb_service.reset.assert_called_once()
        mock_cb_service.force_close.assert_not_called()

    def test_reset_failure_is_reported_not_swallowed(self, service, mock_cb_service):
        """A reset that could not reset says so — the retired fallback returned
        ``success`` after doing the opposite of what was asked."""
        from baldur.services.circuit_breaker.config import CircuitBreakerResult

        mock_cb_service.reset.return_value = CircuitBreakerResult(
            success=False, service_name="payment", error="state not found"
        )

        resp = service._execute_reset(_make_request(action=ControlAPIActions.RESET))

        assert resp.status == "error"
        assert resp.error_code == "CIRCUIT_BREAKER_ERROR"
        assert "state not found" in resp.error_message


class TestResetAgainstTheRealService:
    """The seam the mock above cannot cover.

    Every test in this module drives a MagicMock circuit breaker, which answers
    to any method name — including one that exists nowhere in production. The
    regression is only visible against the real service and a real repository,
    so this class builds both.
    """

    @pytest.fixture
    def real_service(self):
        from baldur.adapters.memory import InMemoryCircuitBreakerStateRepository
        from baldur.services.circuit_breaker import CircuitBreakerService

        cb = CircuitBreakerService(repository=InMemoryCircuitBreakerStateRepository())
        with (
            patch(
                "baldur.services.circuit_breaker.get_circuit_breaker_service",
                return_value=cb,
            ),
            patch("baldur.services.replay_service.ReplayService"),
        ):
            yield ControlAPIService(), cb

    def test_reset_clears_the_manual_override_it_used_to_create(self, real_service):
        """An operator blocks a service, the incident passes, they press Reset.

        Before: Reset pinned ``manually_controlled=True`` with reason
        ``RESET: …``, so the breaker was exempt from automatic protection and
        could never open again — the console showed the row as a standing
        operator override forever, correctly describing a payload that lied.
        """
        service, cb = real_service
        cb.force_open(service_name="payments-api", reason="upstream 5xx storm")
        assert cb.repository.get_by_service_name("payments-api").manually_controlled

        resp = service._execute_reset(
            _make_request(action=ControlAPIActions.RESET, service_name="payments-api")
        )

        assert resp.status == "success"
        state = cb.repository.get_by_service_name("payments-api")
        assert state.manually_controlled is False
        assert state.state == "closed"
        assert state.failure_count == 0


class TestExecuteInjectFailure:
    """_execute_inject_failure()."""

    def test_config_mode(self, service):
        """Configuration mode injection
        Configuration-mode failure injection is applied.
        """
        req = _make_request(
            action=ControlAPIActions.INJECT_FAILURE,
            metadata={"failure_rate": 0.5, "failure_type": "timeout"},
        )
        resp = service._execute_inject_failure(req)
        assert resp.status == "success"
        assert resp.evidence["failure_rate"] == 0.5
        assert resp.evidence["failure_type"] == "timeout"
        # Also recorded in the service's own state
        assert service.is_failure_injection_active("payment")

    def test_trigger_cb_mode(self, service, mock_cb_service):
        """Trigger CB failures mode
        trigger_cb_failures mode calls record_failure.
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
        A TTL sets the injection's expiry.
        """
        req = _make_request(
            action=ControlAPIActions.INJECT_FAILURE,
            ttl_minutes=10,
        )
        resp = service._execute_inject_failure(req)
        assert resp.effective_until is not None


class TestExecuteInjectSuccess:
    """_execute_inject_success()."""

    def test_inject_success(self, service, mock_cb_service):
        """Inject success records successes
        inject_success calls record_success.
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
        A missing success_count in metadata defaults to 1.
        """
        req = _make_request(action=ControlAPIActions.INJECT_SUCCESS)
        service._execute_inject_success(req)
        assert mock_cb_service.record_success.call_count == 1


# =============================================================================
# Helper Method Tests
# =============================================================================


class TestGatherEvidence:
    """_gather_evidence()."""

    def test_gather_evidence_success(self, service, mock_cb_service):
        """Gather evidence success
        Evidence is collected from the circuit breaker state.
        """
        evidence = service._gather_evidence("payment")
        assert "failure_count" in evidence
        assert "success_count" in evidence

    def test_gather_evidence_exception(self, service, mock_cb_service):
        """Gather evidence with exception
        An exception yields an empty dict.
        """
        mock_cb_service.get_or_create_state.side_effect = Exception("Error")
        evidence = service._gather_evidence("payment")
        assert evidence == {}


class TestRecordAudit:
    """_record_audit()."""

    def test_record_audit_no_exception(self, service):
        """Record audit does not raise
        An exception while recording the audit entry does not propagate.
        """
        req = _make_request()
        resp = ControlResponse(status="success", action_applied="allow")
        # Must complete without raising
        service._record_audit(req, resp)


# =============================================================================
# Query Method Tests
# =============================================================================


class TestGetStatus:
    """get_status()."""

    def test_get_status(self, service):
        """Get status returns expected structure
        get_status() returns a dict with the expected shape.
        """
        result = service.get_status(environment="test")
        assert "services" in result
        assert result["environment"] == "test"
        assert "timestamp" in result


class TestGetServiceStatus:
    """get_service_status()."""

    def test_get_service_status(self, service):
        """Get service status
        get_service_status() reports the service's state.
        """
        result = service.get_service_status("payment")
        assert result["service_name"] == "payment"
        assert "state" in result
        assert "failure_count" in result


# =============================================================================
# Failure Injection State Tests
# =============================================================================


class TestFailureInjectionState:
    """is_failure_injection_active / get_failure_injection_config."""

    def test_no_injection_active(self, service):
        """No injection active
        No injection registered returns False.
        """
        assert service.is_failure_injection_active("payment") is False

    def test_injection_active(self, service):
        """Injection active
        An active injection returns True.
        """
        service._failure_injections["payment"] = {"enabled": True}
        assert service.is_failure_injection_active("payment") is True

    def test_injection_disabled(self, service):
        """Injection disabled
        enabled=False returns False.
        """
        service._failure_injections["payment"] = {"enabled": False}
        assert service.is_failure_injection_active("payment") is False

    def test_injection_expired(self, service):
        """Injection expired
        An expired injection returns False and is discarded.
        """
        past = datetime.now() - timedelta(hours=1)
        service._failure_injections["payment"] = {
            "enabled": True,
            "expires_at": past,
        }
        with patch(
            "baldur.services.control_api_service.service.utc_now",
            return_value=datetime.now(),
        ):
            assert service.is_failure_injection_active("payment") is False
        assert "payment" not in service._failure_injections

    def test_get_config_returns_none_for_inactive(self, service):
        """Get config returns None for inactive
        An inactive injection has no config.
        """
        assert service.get_failure_injection_config("payment") is None

    def test_get_config_returns_config_for_active(self, service):
        """Get config returns config for active
        An active injection returns its config.
        """
        config = {"enabled": True, "failure_rate": 0.5}
        service._failure_injections["payment"] = config
        assert service.get_failure_injection_config("payment") == config


# =============================================================================
# Singleton Tests
# =============================================================================


class TestSingleton:
    """The get_control_api_service() singleton."""

    def test_creates_singleton(self):
        """Creates singleton if not exists
        The first call constructs the instance.
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
        A later call returns the existing instance.
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
    """get_metrics()."""

    @patch("baldur.factory.ProviderRegistry")
    @patch("baldur.services.metrics.updaters.update_retry_success_rates")
    @patch("baldur.services.metrics.updaters.update_dlq_pending_gauges")
    @patch("baldur.metrics.registry.get_registered_domains")
    def test_get_metrics_basic(
        self, mock_domains, mock_dlq, mock_retry, mock_registry, service
    ):
        """Get metrics returns expected structure
        get_metrics() returns the expected shape.
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
        A repository exception still yields a well-formed result.
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
        The healthy/degraded counts reflect the circuit breaker states.
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
    """The validation-failure path through execute()."""

    def test_execute_validation_rejection(self, service):
        """Execute returns validation rejection
        A rejection from _validate_request short-circuits execute().
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


# =============================================================================
# 741 D4 — TTL range and type validation at the control-API surface
# =============================================================================


class TestControlRequestTTLValidation:
    """Every action that pins a manual override validates its lifetime.

    The quick-action routes put the raw JSON value straight into
    ``ControlRequest`` with no type check, and ``_validate_request`` runs
    outside ``execute()``'s try/except — so a value that reaches a bare
    comparison here surfaces as a 500 rather than a rejection.
    """

    @pytest.mark.parametrize("action", _TTL_PINNING_ACTIONS)
    @pytest.mark.parametrize("ttl", [1, 60, MAX_MANUAL_OVERRIDE_TTL_MINUTES])
    def test_ttl_inside_the_bound_passes_validation(self, service, action, ttl):
        req = _make_request(
            action=action,
            environment=ControlAPIEnvironments.TEST,
            ttl_minutes=ttl,
        )

        assert service._validate_request(req) is None

    @pytest.mark.parametrize("action", _TTL_PINNING_ACTIONS)
    @pytest.mark.parametrize("ttl", [0, -1, MAX_MANUAL_OVERRIDE_TTL_MINUTES + 1])
    def test_ttl_out_of_range_is_rejected(self, service, action, ttl):
        req = _make_request(
            action=action,
            environment=ControlAPIEnvironments.TEST,
            ttl_minutes=ttl,
        )

        result = service._validate_request(req)

        assert result is not None
        assert result.status == "rejected"
        assert result.error_code == "TTL_OUT_OF_RANGE"

    @pytest.mark.parametrize("action", _TTL_PINNING_ACTIONS)
    @pytest.mark.parametrize("ttl", ["abc", True, 1.5, [5]])
    def test_ttl_out_of_range_covers_non_integer_values(self, service, action, ttl):
        """A non-int is rejected, not compared — comparing it raises."""
        req = _make_request(
            action=action,
            environment=ControlAPIEnvironments.TEST,
            ttl_minutes=ttl,
        )

        result = service._validate_request(req)

        assert result is not None
        assert result.error_code == "TTL_OUT_OF_RANGE"

    @pytest.mark.parametrize("ttl", ["abc", 0, MAX_MANUAL_OVERRIDE_TTL_MINUTES + 1])
    def test_ttl_out_of_range_never_reaches_the_circuit_breaker(
        self, service, mock_cb_service, ttl
    ):
        """Rejected before any state write, and without raising out of execute()."""
        req = _make_request(action=ControlAPIActions.BLOCK, ttl_minutes=ttl)

        resp = service.execute(req)

        assert resp.status == "rejected"
        assert resp.error_code == "TTL_OUT_OF_RANGE"
        mock_cb_service.force_open.assert_not_called()

    def test_ttl_absent_still_passes_for_actions_that_do_not_require_one(self, service):
        """``None`` means "use the configured default" — always acceptable."""
        req = _make_request(
            action=ControlAPIActions.BLOCK,
            environment=ControlAPIEnvironments.TEST,
            ttl_minutes=None,
        )

        assert service._validate_request(req) is None

    def test_ops_override_keeps_its_own_narrower_ttl_limit(self, service):
        """The 60-minute ops rule survives the wider generic bound."""
        req = _make_request(
            action=ControlAPIActions.OVERRIDE,
            environment=ControlAPIEnvironments.OPS,
            ttl_minutes=90,
        )

        result = service._validate_request(req)

        assert result.error_code == "TTL_EXCEEDS_OPS_LIMIT"


# =============================================================================
# 741 D5 — effective_until is read back from storage
# =============================================================================


@pytest.fixture
def cb_repository() -> InMemoryCircuitBreakerStateRepository:
    return InMemoryCircuitBreakerStateRepository()


@pytest.fixture
def live_service(cb_repository):
    """ControlAPIService over a real circuit breaker and repository.

    The response's expiry claim is only meaningful against what the
    repository actually stored, so this path is driven end to end rather
    than against a mocked result object.
    """
    cb = CircuitBreakerService(
        config=CircuitBreakerConfig(enabled=True, manual_override_ttl_minutes=45),
        repository=cb_repository,
    )
    with (
        patch(
            "baldur.services.circuit_breaker.get_circuit_breaker_service",
            return_value=cb,
        ),
        patch(
            "baldur.services.replay_service.ReplayService",
            return_value=MagicMock(spec=ReplayService),
        ),
        patch(
            "baldur.services.circuit_breaker.manual_control._is_system_enabled",
            return_value=True,
        ),
    ):
        yield ControlAPIService()


class TestControlResponseEffectiveUntil:
    """The reported lift time is the stored one, for every pinning action."""

    @pytest.mark.parametrize(
        "action",
        [ControlAPIActions.BLOCK, ControlAPIActions.ALLOW, ControlAPIActions.OVERRIDE],
    )
    def test_effective_until_equals_the_stored_expiry(
        self, live_service, cb_repository, action
    ):
        req = _make_request(action=action, ttl_minutes=30)

        resp = live_service.execute(req)

        stored = cb_repository.get_by_service_name("payment")
        assert resp.status == "success"
        assert resp.effective_until == stored.manual_override_expires_at.isoformat()

    def test_blank_ttl_stores_and_reports_the_configured_default(
        self, live_service, cb_repository
    ):
        """No typed TTL — the settings default is what lands in storage."""
        before = utc_now()
        req = _make_request(action=ControlAPIActions.BLOCK, ttl_minutes=None)

        resp = live_service.execute(req)

        stored = cb_repository.get_by_service_name("payment")
        expiry = stored.manual_override_expires_at
        assert resp.effective_until == expiry.isoformat()
        assert (
            before + timedelta(minutes=45)
            <= expiry
            <= utc_now() + timedelta(minutes=45)
        )

    def test_allow_now_carries_an_expiry_it_never_had(
        self, live_service, cb_repository
    ):
        """A "temporary" force-allow used to suspend protection permanently."""
        req = _make_request(action=ControlAPIActions.ALLOW, ttl_minutes=None)

        resp = live_service.execute(req)

        stored = cb_repository.get_by_service_name("payment")
        assert stored.manual_override_expires_at is not None
        assert resp.effective_until == stored.manual_override_expires_at.isoformat()
