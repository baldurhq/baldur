"""
CircuitBreakerPolicy hook integration + _should_open_circuit window cap + Protocol inheritance (#227).

Test targets:
- policy.py: _invoke_hooks(), the hooks parameter, the breaker-service binding,
  ResiliencePolicy[T] inheritance
- service.py: _should_open_circuit() — the sliding_window_size cap

Source basis:
- CircuitBreakerPolicy._invoke_hooks(): every hook is invoked fail-open
- execute(): hooks fire at on_execute (start), on_reject (CB OPEN), on_success,
  on_failure
- CircuitBreakerPolicy(ResiliencePolicy[T]): explicit Protocol inheritance →
  isinstance() passes
- hooks parameter: None means an empty list (transition-only, #494); external
  authors inject ``hooks=[…]``
- breaker-service binding: neither ``cb_service`` nor ``config`` → the
  process-shared service, resolved per access; either one → a private instance
- _should_open_circuit(): with sliding_window_size > 0 and total_calls >
  window_size the cap applies; the count-based threshold reads the raw
  failure_count

UNIT_TEST_GUIDELINES.md compliance:
- Contract: Protocol inheritance, hooks default
- Behavior: source-referenced, mock call-order assertions
- conftest placement: single-file fixtures live in the file (§5.1)
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from baldur.interfaces.resilience_policy import (
    PolicyOutcome,
    PolicyResult,
    ResiliencePolicy,
)
from baldur.services.circuit_breaker.config import (
    CircuitBreakerConfig,
    CircuitBreakerDecision,
)
from baldur.services.circuit_breaker.exceptions import CircuitBreakerOpenError
from baldur.services.circuit_breaker.policy import (
    CircuitBreakerPolicy,
)
from baldur.services.event_bus import EventType


def _reject_decision(state_str: str = "open") -> CircuitBreakerDecision:
    """Helper for D2 reject mock setup — see test_circuit_breaker_policy.py."""
    return CircuitBreakerDecision(
        allowed=False,
        state=MagicMock(state=state_str),
    )


# =============================================================================
# Fixtures — used by this file only, so they live here (§5.1)
# =============================================================================


@pytest.fixture
def mock_cb_service():
    """CircuitBreakerService mock — default behaviour: enabled + allow.

    Post-#485 D2: ``CircuitBreakerPolicy.execute`` calls
    ``should_allow_with_state`` (companion API). The default mock returns a
    CLOSED admit decision; reject-path tests override it.
    """
    service = MagicMock()
    service.is_enabled = True
    service.should_allow.return_value = True
    service.should_allow_with_state.return_value = CircuitBreakerDecision(
        allowed=True,
        state=MagicMock(state="closed"),
    )
    service.get_state.return_value = "closed"
    service.record_success.return_value = None
    service.record_failure.return_value = None
    return service


@pytest.fixture
def mock_hook():
    """A generic mock hook — a MagicMock carrying every hook method."""
    hook = MagicMock()
    hook.on_execute = MagicMock()
    hook.on_success = MagicMock()
    hook.on_failure = MagicMock()
    hook.on_retry = MagicMock()
    hook.on_reject = MagicMock()
    return hook


@pytest.fixture
def policy_with_mock_hook(mock_cb_service, mock_hook):
    """A CircuitBreakerPolicy with ``mock_hook`` injected."""
    return CircuitBreakerPolicy(
        service_name="test_api",
        cb_service=mock_cb_service,
        hooks=[mock_hook],
    )


# =============================================================================
# ResiliencePolicy[T] Protocol inheritance (Contract)
# =============================================================================


class TestCircuitBreakerPolicyProtocolContract:
    """CircuitBreakerPolicy explicitly inherits the ResiliencePolicy[T] Protocol."""

    def test_isinstance_resilience_policy(self, mock_cb_service):
        """A CircuitBreakerPolicy instance passes the ResiliencePolicy isinstance check."""
        policy = CircuitBreakerPolicy(
            service_name="test",
            cb_service=mock_cb_service,
        )
        assert isinstance(policy, ResiliencePolicy)

    def test_has_name_property(self, mock_cb_service):
        """ResiliencePolicy Protocol requirement: a ``name`` property exists."""
        policy = CircuitBreakerPolicy(
            service_name="test",
            cb_service=mock_cb_service,
        )
        assert hasattr(policy, "name")
        assert isinstance(policy.name, str)

    def test_has_execute_method(self, mock_cb_service):
        """ResiliencePolicy Protocol requirement: an ``execute`` method exists."""
        policy = CircuitBreakerPolicy(
            service_name="test",
            cb_service=mock_cb_service,
        )
        assert hasattr(policy, "execute")
        assert callable(policy.execute)

    def test_mro_includes_resilience_policy(self):
        """ResiliencePolicy appears in CircuitBreakerPolicy's MRO."""
        mro_names = [cls.__name__ for cls in CircuitBreakerPolicy.__mro__]
        assert "ResiliencePolicy" in mro_names


# =============================================================================
# hooks parameter (Contract)
# =============================================================================


class TestCircuitBreakerPolicyHooksParamContract:
    """The hooks parameter's default and injection contract."""

    def test_default_hooks_is_empty(self, mock_cb_service):
        """hooks=None → an empty list (transition-only, #494)."""
        policy = CircuitBreakerPolicy(
            service_name="test",
            cb_service=mock_cb_service,
        )
        # Post-#494: per-reject hook bodies live nowhere by default. State
        # transitions are published by ``CircuitBreakerService``;
        # ``baldur_circuit_breaker_blocked_total`` covers per-reject volume.
        assert policy._hooks == []

    def test_custom_hooks_override_defaults(self, mock_cb_service):
        """hooks=[custom] → ``_hooks`` holds the custom hook."""
        custom_hook = MagicMock()
        policy = CircuitBreakerPolicy(
            service_name="test",
            cb_service=mock_cb_service,
            hooks=[custom_hook],
        )
        assert policy._hooks == [custom_hook]

    def test_empty_hooks_list_accepted(self, mock_cb_service):
        """hooks=[] → an empty list is set (no hooks)."""
        policy = CircuitBreakerPolicy(
            service_name="test",
            cb_service=mock_cb_service,
            hooks=[],
        )
        assert policy._hooks == []


# =============================================================================
# _invoke_hooks (Behavior)
# =============================================================================


class TestInvokeHooksBehavior:
    """_invoke_hooks() is fail-open."""

    def test_calls_hook_method_with_args(self, mock_cb_service):
        """_invoke_hooks() calls the named method with the given arguments."""
        hook = MagicMock()
        policy = CircuitBreakerPolicy(
            service_name="test",
            cb_service=mock_cb_service,
            hooks=[hook],
        )
        policy._invoke_hooks("on_execute", "test_service", 1)
        hook.on_execute.assert_called_once_with("test_service", 1)

    def test_calls_all_hooks(self, mock_cb_service):
        """Every hook is called when several are registered."""
        hook1 = MagicMock()
        hook2 = MagicMock()
        policy = CircuitBreakerPolicy(
            service_name="test",
            cb_service=mock_cb_service,
            hooks=[hook1, hook2],
        )
        policy._invoke_hooks("on_reject", "svc", "reason")
        hook1.on_reject.assert_called_once_with("svc", "reason")
        hook2.on_reject.assert_called_once_with("svc", "reason")

    def test_fail_open_swallows_hook_exception(self, mock_cb_service):
        """Fail-open: a hook exception does not abort _invoke_hooks()."""
        hook = MagicMock()
        hook.on_execute.side_effect = RuntimeError("hook crashed")
        policy = CircuitBreakerPolicy(
            service_name="test",
            cb_service=mock_cb_service,
            hooks=[hook],
        )
        # The exception must not propagate
        policy._invoke_hooks("on_execute", "svc", 1)

    def test_subsequent_hooks_called_after_first_fails(self, mock_cb_service):
        """A failing first hook does not stop the hooks after it."""
        hook1 = MagicMock()
        hook1.on_reject.side_effect = RuntimeError("hook1 failed")
        hook2 = MagicMock()
        policy = CircuitBreakerPolicy(
            service_name="test",
            cb_service=mock_cb_service,
            hooks=[hook1, hook2],
        )
        policy._invoke_hooks("on_reject", "svc", "reason")
        # hook2 is still called after hook1 failed
        hook2.on_reject.assert_called_once_with("svc", "reason")

    def test_empty_hooks_no_error(self, mock_cb_service):
        """With hooks=[] _invoke_hooks() returns without error."""
        policy = CircuitBreakerPolicy(
            service_name="test",
            cb_service=mock_cb_service,
            hooks=[],
        )
        policy._invoke_hooks("on_execute", "svc", 1)


# =============================================================================
# execute() hook call points (Behavior)
# =============================================================================


class TestPolicyExecuteHooksIntegrationBehavior:
    """execute() fires each hook at the right point."""

    def test_on_execute_called_when_cb_enabled(self, policy_with_mock_hook, mock_hook):
        """on_execute fires when the CB is enabled."""
        policy_with_mock_hook.execute(lambda: "ok")
        mock_hook.on_execute.assert_called_once_with("test_api", 1)

    def test_on_execute_not_called_when_cb_disabled(self, mock_hook):
        """on_execute does not fire when the CB is disabled (early return)."""
        disabled_service = MagicMock()
        disabled_service.is_enabled = False
        policy = CircuitBreakerPolicy(
            service_name="test_api",
            cb_service=disabled_service,
            hooks=[mock_hook],
        )
        policy.execute(lambda: "ok")
        mock_hook.on_execute.assert_not_called()

    def test_on_reject_called_when_should_allow_false(self, mock_cb_service, mock_hook):
        """on_reject fires when should_allow() is False."""
        mock_cb_service.should_allow.return_value = False
        mock_cb_service.should_allow_with_state.return_value = _reject_decision()
        policy = CircuitBreakerPolicy(
            service_name="test_api",
            cb_service=mock_cb_service,
            hooks=[mock_hook],
        )
        policy.execute(lambda: "ok")
        mock_hook.on_reject.assert_called_once_with("test_api", "circuit_open")

    def test_on_success_called_on_successful_execution(
        self, policy_with_mock_hook, mock_hook
    ):
        """on_success fires on success."""
        policy_with_mock_hook.execute(lambda: "result")
        assert mock_hook.on_success.call_count == 1
        call_args = mock_hook.on_success.call_args
        assert call_args[0][0] == "test_api"
        # The second argument is the PolicyResult
        assert isinstance(call_args[0][1], PolicyResult)

    def test_on_failure_called_on_exception(self, policy_with_mock_hook, mock_hook):
        """on_failure fires on failure."""
        with pytest.raises(ValueError):
            policy_with_mock_hook.execute(
                lambda: (_ for _ in ()).throw(ValueError("fail"))
            )
        mock_hook.on_failure.assert_called_once()
        call_args = mock_hook.on_failure.call_args
        assert call_args[0][0] == "test_api"
        assert isinstance(call_args[0][1], ValueError)
        assert call_args[0][2] == 1  # attempt

    def test_on_success_not_called_on_rejection(self, mock_cb_service, mock_hook):
        """on_success does not fire on a rejection."""
        mock_cb_service.should_allow.return_value = False
        mock_cb_service.should_allow_with_state.return_value = _reject_decision()
        policy = CircuitBreakerPolicy(
            service_name="test_api",
            cb_service=mock_cb_service,
            hooks=[mock_hook],
        )
        policy.execute(lambda: "ok")
        mock_hook.on_success.assert_not_called()

    def test_on_failure_not_called_on_rejection(self, mock_cb_service, mock_hook):
        """on_failure does not fire on a rejection."""
        mock_cb_service.should_allow.return_value = False
        mock_cb_service.should_allow_with_state.return_value = _reject_decision()
        policy = CircuitBreakerPolicy(
            service_name="test_api",
            cb_service=mock_cb_service,
            hooks=[mock_hook],
        )
        policy.execute(lambda: "ok")
        mock_hook.on_failure.assert_not_called()

    def test_hook_failure_does_not_affect_execution_result(self, mock_cb_service):
        """A hook exception does not affect the execute() result (fail-open)."""
        failing_hook = MagicMock()
        failing_hook.on_execute.side_effect = RuntimeError("hook crash")
        failing_hook.on_success.side_effect = RuntimeError("hook crash")
        policy = CircuitBreakerPolicy(
            service_name="test_api",
            cb_service=mock_cb_service,
            hooks=[failing_hook],
        )
        result = policy.execute(lambda: "safe_result")
        assert result.value == "safe_result"
        assert result.outcome == PolicyOutcome.SUCCESS

    def test_hook_failure_on_reject_does_not_affect_result(self, mock_cb_service):
        """A failing on_reject hook does not affect the rejection result."""
        mock_cb_service.should_allow.return_value = False
        mock_cb_service.should_allow_with_state.return_value = _reject_decision()
        failing_hook = MagicMock()
        failing_hook.on_execute = MagicMock()
        failing_hook.on_reject.side_effect = RuntimeError("hook crash")
        policy = CircuitBreakerPolicy(
            service_name="test_api",
            cb_service=mock_cb_service,
            hooks=[failing_hook],
        )
        result = policy.execute(lambda: "ok")
        assert result.outcome == PolicyOutcome.REJECTED
        assert isinstance(result.error, CircuitBreakerOpenError)


# =============================================================================
# Breaker-service binding (Behavior)
# =============================================================================


class TestCircuitBreakerPolicyServiceBindingBehavior:
    """Which CircuitBreakerService a policy records on, by constructor form.

    The default form binds the process-shared service, so every reader of
    that service's rate evidence sees the traffic the policy admitted; either
    ``cb_service`` or ``config`` opts the policy out into a private instance
    whose evidence stays with it.
    """

    def test_the_default_form_resolves_the_process_shared_service(self):
        """Neither ``cb_service`` nor ``config`` → the runtime singleton, by identity."""
        from baldur.services.circuit_breaker.convenience import (
            get_circuit_breaker_service,
        )

        policy = CircuitBreakerPolicy(service_name="test_api")

        assert policy.cb_service is get_circuit_breaker_service()

    def test_an_injected_service_is_bound_as_given(self, mock_cb_service):
        """``cb_service=`` → that object, not the singleton."""
        from baldur.services.circuit_breaker.convenience import (
            get_circuit_breaker_service,
        )

        policy = CircuitBreakerPolicy(
            service_name="test_api", cb_service=mock_cb_service
        )

        assert policy.cb_service is mock_cb_service
        assert policy.cb_service is not get_circuit_breaker_service()

    def test_a_pinned_config_builds_a_private_service_carrying_it(self):
        """``config=`` without ``cb_service`` → a private instance on that config."""
        from baldur.services.circuit_breaker.convenience import (
            get_circuit_breaker_service,
        )
        from baldur.services.circuit_breaker.service import CircuitBreakerService

        config = CircuitBreakerConfig(failure_threshold=10)
        policy = CircuitBreakerPolicy(service_name="test_api", config=config)

        assert isinstance(policy.cb_service, CircuitBreakerService)
        assert policy.cb_service is not get_circuit_breaker_service()
        assert policy.cb_service.config.failure_threshold == 10

    def test_an_injected_service_outranks_a_pinned_config(self, mock_cb_service):
        """Both given → ``cb_service`` wins and the config builds nothing."""
        config = CircuitBreakerConfig(failure_threshold=10)
        policy = CircuitBreakerPolicy(
            service_name="test_api", cb_service=mock_cb_service, config=config
        )

        assert policy.cb_service is mock_cb_service


# =============================================================================
# _should_open_circuit threshold boundaries (§8.1)
# =============================================================================


class TestEvaluateTripThresholdBoundaries:
    """Exact-boundary coverage for the rate-based trip gate.

    A ``>=`` to ``>`` drift on the rate gate still trips at 60% and goes
    undetected, yet at exactly the threshold it would fail to trip — a failing
    dependency held open one tick too long. Pin the gate at its exact boundary.
    """

    @staticmethod
    def _config(**cfg):
        base = {"enabled": True}
        base.update(cfg)
        return CircuitBreakerConfig(**base)

    def test_rate_threshold_trips_exactly_at_threshold(self):
        """failure_rate == failure_rate_threshold trips (the ``>=`` boundary)."""
        from baldur.services.circuit_breaker.outcome_window import (
            TRIP_REASON_RATE,
            evaluate_trip,
        )

        # count gate held out of reach so only the rate gate decides.
        config = self._config(
            failure_threshold=100, failure_rate_threshold=50.0, minimum_calls=1
        )

        # at the boundary: 5 failures of 10 calls == 50%
        assert (
            evaluate_trip(
                consecutive_failures=0,
                window_failures=5,
                window_total=10,
                config=config,
            )
            == TRIP_REASON_RATE
        )
        # below the boundary: 4 failures of 10 calls == 40%
        assert (
            evaluate_trip(
                consecutive_failures=0,
                window_failures=4,
                window_total=10,
                config=config,
            )
            is None
        )


# =============================================================================
# _evaluate_admission gates (downstream checker + recovery-timeout boundary)
# =============================================================================


class TestEvaluateAdmissionGates:
    """Admission gates that survived the full circuit-breaker suite: the
    downstream-checker polarity and the OPEN recovery-timeout boundary."""

    @staticmethod
    def _service(**cfg):
        from baldur.adapters.memory.circuit_breaker import (
            InMemoryCircuitBreakerStateRepository,
        )
        from baldur.services.circuit_breaker.service import CircuitBreakerService

        base = {"enabled": True}
        base.update(cfg)
        return CircuitBreakerService(
            config=CircuitBreakerConfig(**base),
            repository=InMemoryCircuitBreakerStateRepository(),
        )

    def test_downstream_checker_false_preempts_to_fallback(self):
        """A registered downstream checker returning False preempts the call
        (``allowed=False``); returning True does not block a healthy CLOSED
        circuit. Pins the ``if not checker`` polarity — a flip would invert
        fail-open vs fail-closed on the preemptive-fallback path."""
        svc = self._service()

        svc.register_downstream_checker(lambda service_name: True)
        assert svc.should_allow_with_state("svc").allowed is True

        svc.register_downstream_checker(lambda service_name: False)
        assert svc.should_allow_with_state("svc").allowed is False

    def test_open_circuit_at_exact_recovery_timeout_admits_probe(self):
        """At elapsed == recovery_timeout the OPEN circuit is no longer
        short-circuit rejected (strict ``<``): it proceeds to acquire a
        half-open probe slot and admits. A ``<`` → ``<=`` drift would block
        recovery one tick too long."""
        from datetime import UTC, datetime, timedelta
        from unittest.mock import patch

        svc = self._service(recovery_timeout=60, half_open_max_calls=1)
        t0 = datetime(2026, 1, 1, tzinfo=UTC)
        # Materialize the entry first; update_state only persists an existing one.
        svc.get_or_create_state("svc")
        svc.repository.update_state(service_name="svc", state="open", opened_at=t0)

        # elapsed = now(t0 + 60) - opened_at(t0) == recovery_timeout(60) exactly.
        with patch(
            "baldur.services.circuit_breaker.service.utc_now",
            return_value=t0 + timedelta(seconds=60),
        ):
            decision = svc.should_allow_with_state("svc")

        assert decision.allowed is True


# =============================================================================
# Per-transition emission guard (#494 D2 regression-prevention)
# =============================================================================


class TestCBOpenedEmittedPerTransitionBehavior:
    """``CIRCUIT_BREAKER_OPENED`` is emitted per state-transition, not per-reject.

    Post-#494 the policy default ``hooks=[]`` removes per-reject EventBus
    emission. The publisher-side contract is: a single ``closed→open``
    transition must emit ``CIRCUIT_BREAKER_OPENED`` exactly once even when N
    subsequent ``policy.execute()`` calls are rejected while the breaker
    sits in OPEN. (A separate ``half_open→open`` re-trip would legitimately
    emit again — that is out of scope for this test.)
    """

    def test_one_transition_n_rejects_emits_once(self):
        from baldur.adapters.memory.circuit_breaker import (
            InMemoryCircuitBreakerStateRepository,
        )
        from baldur.services.circuit_breaker.service import (
            CircuitBreakerService,
        )

        repo = InMemoryCircuitBreakerStateRepository()
        config = CircuitBreakerConfig(
            enabled=True,
            failure_threshold=2,
            failure_rate_threshold=0.0,
            minimum_calls=1,
            sliding_window_size=10,
            recovery_timeout=86_400,
        )
        cb_service = CircuitBreakerService(config=config, repository=repo)

        bus = MagicMock()
        cb_service._event_bus = bus

        policy = CircuitBreakerPolicy(
            service_name="svc",
            cb_service=cb_service,
        )

        def boom():
            raise RuntimeError("trip")

        # Drive failures up to threshold — second failure trips closed→open.
        for _ in range(2):
            with pytest.raises(RuntimeError):
                policy.execute(boom)

        # N rejected calls — CB is OPEN with bumped recovery_timeout.
        for _ in range(20):
            result = policy.execute(lambda: "ok")
            assert result.outcome == PolicyOutcome.REJECTED

        opened_emits = [
            call
            for call in bus.emit.call_args_list
            if call.args and call.args[0] == EventType.CIRCUIT_BREAKER_OPENED
        ]
        assert len(opened_emits) == 1, (
            f"expected exactly 1 CIRCUIT_BREAKER_OPENED emission, got "
            f"{len(opened_emits)}"
        )
