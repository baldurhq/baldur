"""Observe-only (dry-run) behaviour for the Circuit Breaker surfaces (doc 603).

Three CB-tier intervention sites gated on the shared ``intervention_suppressed``
predicate (D5) plus the manual-override WARNING (D6):

- ``CircuitBreakerPolicy.execute`` — no reject on OPEN, no record_* transition,
  the business fn runs once, outcome is never REJECTED.
- ``ProtectionMixin.record_rate_limit_response`` — the 429 auto force-open is
  skipped (returns None); the 429 tracking (observation) still runs.
- ``ManualControlMixin.force_open`` / ``force_close`` — stay LIVE by design
  (manual intent), but emit an in-band WARNING under observe-only.

Behaviors are computed from source (PolicyOutcome.*, observe-only gate), so
these are Behavior-class tests.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import structlog

from baldur.adapters.memory.circuit_breaker import (
    InMemoryCircuitBreakerStateRepository,
)
from baldur.core.execution_mode import (
    ExecutionMode,
    clear_execution_mode_override,
    set_execution_mode,
)
from baldur.interfaces.resilience_policy import PolicyOutcome
from baldur.services.circuit_breaker.config import CircuitBreakerConfig
from baldur.services.circuit_breaker.manual_control import (
    _warn_if_manual_override_under_dry_run,
)
from baldur.services.circuit_breaker.policy import CircuitBreakerPolicy
from baldur.services.circuit_breaker.service import CircuitBreakerService
from tests.factories import (
    InMemoryCircuitBreakerRepository,
    InMemoryRateLimitTracker,
    dry_run_active,
)


def _mock_cb_service(allowed: bool) -> MagicMock:
    """A CircuitBreakerService mock whose should_allow decision is fixed."""
    cb = MagicMock(spec=CircuitBreakerService)
    cb.is_enabled = True
    state_value = "open" if not allowed else "closed"
    cb.should_allow_with_state.return_value = SimpleNamespace(
        allowed=allowed,
        state=SimpleNamespace(state=state_value),
    )
    # Observe-only peeks the state read-only via get_or_create_state (the gate
    # is resolved before the mutating should_allow_with_state); keep it
    # consistent with the admission decision above.
    cb.get_or_create_state.return_value = SimpleNamespace(state=state_value)
    return cb


class TestCBPolicyDryRun:
    """CircuitBreakerPolicy.execute observe-only branch."""

    def test_open_circuit_does_not_reject_under_dry_run(self):
        # Given an OPEN circuit (would normally REJECT) and dry-run active
        cb = _mock_cb_service(allowed=False)
        policy = CircuitBreakerPolicy(service_name="payment-api", cb_service=cb)
        # When the protected call runs under observe-only
        with dry_run_active():
            result = policy.execute(lambda: "ok")
        # Then it is NOT rejected — the business fn ran and SUCCESS is returned
        assert result.outcome == PolicyOutcome.SUCCESS
        assert result.value == "ok"

    def test_does_not_record_success_transition_under_dry_run(self):
        cb = _mock_cb_service(allowed=True)
        policy = CircuitBreakerPolicy(service_name="payment-api", cb_service=cb)
        with dry_run_active():
            policy.execute(lambda: "ok")
        cb.record_success.assert_not_called()
        cb.record_failure.assert_not_called()

    def test_does_not_record_failure_and_propagates_business_exception(self):
        # Under observe-only a failing business call still raises (the exception
        # propagates upstream) but the CB failure transition is NOT recorded.
        cb = _mock_cb_service(allowed=True)
        policy = CircuitBreakerPolicy(service_name="payment-api", cb_service=cb)

        def boom():
            raise ValueError("downstream failed")

        with dry_run_active(), pytest.raises(ValueError, match="downstream failed"):
            policy.execute(boom)
        cb.record_failure.assert_not_called()

    def test_business_fn_runs_exactly_once_under_dry_run(self):
        cb = _mock_cb_service(allowed=False)
        policy = CircuitBreakerPolicy(service_name="payment-api", cb_service=cb)
        calls = {"n": 0}

        def fn():
            calls["n"] += 1
            return "v"

        with dry_run_active():
            policy.execute(fn)
        assert calls["n"] == 1

    def test_open_circuit_rejects_when_not_dry_run(self):
        # Control: without dry-run the same OPEN circuit REJECTS — proving the
        # gate (not some unrelated short-circuit) is what changes the outcome.
        cb = _mock_cb_service(allowed=False)
        policy = CircuitBreakerPolicy(service_name="payment-api", cb_service=cb)
        result = policy.execute(lambda: "ok")
        assert result.outcome == PolicyOutcome.REJECTED

    def test_logs_would_have_decision_under_dry_run(self):
        cb = _mock_cb_service(allowed=False)
        policy = CircuitBreakerPolicy(service_name="payment-api", cb_service=cb)
        with dry_run_active(), structlog.testing.capture_logs() as logs:
            policy.execute(lambda: "ok")
        would_have = [
            e
            for e in logs
            if e.get("event") == "execution_mode.intervention_suppressed"
        ]
        assert len(would_have) == 1
        assert would_have[0]["action"] == "circuit_breaker_reject"
        assert would_have[0]["would_reject"] is True

    def test_open_recovery_does_not_mutate_state_under_dry_run(self):
        # Regression: the observe-only gate must be resolved BEFORE
        # should_allow_with_state, whose admission path atomically advances
        # OPEN->HALF_OPEN (a real persisted transition + auto-recovery audit +
        # CIRCUIT_BREAKER_HALF_OPENED event) once recovery_timeout elapses. A
        # real service (not a mock) is required to exercise that path:
        # recovery_timeout=0 makes an OPEN circuit immediately recovery-eligible.
        repo = InMemoryCircuitBreakerStateRepository()
        service = CircuitBreakerService(
            config=CircuitBreakerConfig(enabled=True, recovery_timeout=0),
            repository=repo,
        )
        service.force_open("payment-api", reason="setup")
        assert repo.get_by_service_name("payment-api").state == "open"

        policy = CircuitBreakerPolicy(service_name="payment-api", cb_service=service)
        with dry_run_active(), structlog.testing.capture_logs() as logs:
            result = policy.execute(lambda: "ok")

        # State stays OPEN — no OPEN->HALF_OPEN auto-recovery transition leaked,
        assert repo.get_by_service_name("payment-api").state == "open"
        # and no auto-recovery transition audit row fired under observe-only.
        half_open_audits = [
            e
            for e in logs
            if e.get("event") == "cb_audit.event" and e.get("new_state") == "half_open"
        ]
        assert half_open_audits == []
        assert result.outcome == PolicyOutcome.SUCCESS
        assert result.value == "ok"

    def test_open_recovery_transitions_when_not_dry_run(self):
        # Control: the same setup WITHOUT dry-run advances the state machine out
        # of OPEN — proving observe-only (not an inert repo) is what holds the
        # state in the regression test above.
        repo = InMemoryCircuitBreakerStateRepository()
        service = CircuitBreakerService(
            config=CircuitBreakerConfig(enabled=True, recovery_timeout=0),
            repository=repo,
        )
        service.force_open("payment-api", reason="setup")
        policy = CircuitBreakerPolicy(service_name="payment-api", cb_service=service)
        policy.execute(lambda: "ok")
        assert repo.get_by_service_name("payment-api").state != "open"


class TestProtectionDryRun:
    """ProtectionMixin.record_rate_limit_response observe-only branch."""

    @staticmethod
    def _cascade_tracker() -> MagicMock:
        # 15 rate-limits / 100 requests = 15% > 10% threshold → cascade.
        tracker = MagicMock(spec=InMemoryRateLimitTracker)
        tracker.get_rate_limit_count.return_value = 15
        tracker.get_request_count.return_value = 100
        return tracker

    @staticmethod
    def _service() -> CircuitBreakerService:
        config = CircuitBreakerConfig(
            enabled=True,
            rate_limit_cascade_threshold=10,
            rate_limit_cascade_window_seconds=60,
            rate_limit_cascade_rate=10.0,
            rate_limit_cascade_minimum_calls=20,
        )
        return CircuitBreakerService(
            config=config, repository=InMemoryCircuitBreakerRepository()
        )

    def test_force_open_skipped_and_returns_none_under_dry_run(self):
        tracker = self._cascade_tracker()
        service = self._service()
        with (
            patch(
                "baldur.services.circuit_breaker.protection.get_rate_limit_tracker",
                return_value=tracker,
            ),
            patch.object(service, "force_open") as mock_force,
            dry_run_active(),
        ):
            result = service.record_rate_limit_response("payment-api")
        assert result is None
        mock_force.assert_not_called()

    def test_429_tracking_still_runs_under_dry_run(self):
        # Observation is kept: the 429 + request are recorded even though the
        # force-open intervention is suppressed.
        tracker = self._cascade_tracker()
        service = self._service()
        with (
            patch(
                "baldur.services.circuit_breaker.protection.get_rate_limit_tracker",
                return_value=tracker,
            ),
            patch.object(service, "force_open"),
            dry_run_active(),
        ):
            service.record_rate_limit_response("payment-api")
        tracker.record_rate_limit.assert_called_once_with("payment-api")
        tracker.record_request.assert_called_once_with("payment-api")

    def test_force_open_invoked_when_not_dry_run(self):
        # Control: same cascade without dry-run DOES force-open.
        tracker = self._cascade_tracker()
        service = self._service()
        with (
            patch(
                "baldur.services.circuit_breaker.protection.get_rate_limit_tracker",
                return_value=tracker,
            ),
            patch.object(
                service,
                "force_open",
                return_value=SimpleNamespace(success=True),
            ) as mock_force,
        ):
            result = service.record_rate_limit_response("payment-api")
        mock_force.assert_called_once()
        assert result is not None


class TestManualOverrideDryRunWarning:
    """D6 — manual force stays live but warns under observe-only."""

    def teardown_method(self):
        clear_execution_mode_override()

    def test_warns_under_observe_only(self):
        set_execution_mode(ExecutionMode.shadow())
        with structlog.testing.capture_logs() as logs:
            _warn_if_manual_override_under_dry_run("payment-api", "force_open")
        events = [
            e
            for e in logs
            if e.get("event") == "system_control.manual_override_under_dry_run"
        ]
        assert len(events) == 1
        assert events[0]["manual_control_action"] == "force_open"
        assert events[0]["service_name"] == "payment-api"

    def test_silent_when_executing(self):
        set_execution_mode(ExecutionMode.active())
        with structlog.testing.capture_logs() as logs:
            _warn_if_manual_override_under_dry_run("payment-api", "force_open")
        events = [
            e
            for e in logs
            if e.get("event") == "system_control.manual_override_under_dry_run"
        ]
        assert events == []

    def test_silent_when_resolver_raises(self):
        # Fail-safe: a resolver error leaves the warning silent (force still runs).
        with patch(
            "baldur.services.circuit_breaker.manual_control.get_execution_mode",
            side_effect=RuntimeError("resolver down"),
        ):
            with structlog.testing.capture_logs() as logs:
                _warn_if_manual_override_under_dry_run("svc", "force_close")
        assert logs == []

    def test_force_open_still_executes_under_dry_run(self):
        # No behavioral change: the manual force opens the circuit AND warns.
        service = CircuitBreakerService(
            config=CircuitBreakerConfig(enabled=True),
            repository=InMemoryCircuitBreakerRepository(),
        )
        with (
            patch(
                "baldur.services.circuit_breaker.manual_control._is_system_enabled",
                return_value=True,
            ),
            dry_run_active(),
            structlog.testing.capture_logs() as logs,
        ):
            result = service.force_open("payment-api", reason="manual stop")
        assert result.success is True
        warnings = [
            e
            for e in logs
            if e.get("event") == "system_control.manual_override_under_dry_run"
        ]
        assert len(warnings) == 1
