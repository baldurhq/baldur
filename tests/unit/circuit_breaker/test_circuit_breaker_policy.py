"""
CircuitBreakerPolicy unit tests (#227).

Test targets:
- services/circuit_breaker/policy.py (CircuitBreakerPolicy, the circuit_breaker decorator)
- services/circuit_breaker/exceptions.py (CircuitBreakerOpenError)

Source basis:
- ``name`` == "circuit_breaker"
- CB disabled → the function runs directly, SUCCESS
- should_allow() == False → REJECTED + CircuitBreakerOpenError
- success → record_success() → SUCCESS
- failure → _is_failure() decides → record_failure() → the exception re-raises
- ignore_exceptions → record_failure() is not called
- failure_exceptions filtering
- the @circuit_breaker() decorator
- CircuitBreakerOpenError attributes

UNIT_TEST_GUIDELINES.md compliance:
- Contract: hardcoded expectations (name, outcome, executed_policies)
- Behavior: source-referenced (config, constants)
- conftest placement: single-file fixtures live in the file
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from baldur.interfaces.resilience_policy import (
    PolicyContext,
    PolicyOutcome,
    PolicyResult,
)
from baldur.services.circuit_breaker.config import CircuitBreakerDecision
from baldur.services.circuit_breaker.exceptions import CircuitBreakerOpenError
from baldur.services.circuit_breaker.policy import (
    CircuitBreakerPolicy,
    circuit_breaker,
)


def _reject_decision(state_str: str = "open") -> CircuitBreakerDecision:
    """Helper for D2 reject mock setup — returns a False decision with the
    given string state. Used inline at sites that previously set
    ``should_allow.return_value = False`` and (optionally)
    ``get_state.return_value = "open"``.
    """
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
    ``should_allow_with_state`` (companion API) instead of the old
    ``should_allow`` + ``get_state`` pair, so the default mock pre-configures
    a CLOSED admit decision. Tests that exercise the reject path override
    ``should_allow_with_state.return_value`` with a False/state="open"
    ``CircuitBreakerDecision``.
    """
    from baldur.services.circuit_breaker.config import CircuitBreakerDecision

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
def policy(mock_cb_service):
    """The default CircuitBreakerPolicy instance."""
    return CircuitBreakerPolicy(
        service_name="test_api",
        cb_service=mock_cb_service,
    )


@pytest.fixture
def disabled_cb_service():
    """A mock service with the CB disabled."""
    service = MagicMock()
    service.is_enabled = False
    return service


# =============================================================================
# Contract
# =============================================================================


class TestCircuitBreakerPolicyContract:
    """CircuitBreakerPolicy's fixed identifiers and result-shape contract."""

    def test_name_is_circuit_breaker(self, policy):
        """The ``name`` property is 'circuit_breaker'."""
        assert policy.name == "circuit_breaker"

    def test_service_name_property(self, policy):
        """The ``service_name`` property is the constructor argument."""
        assert policy.service_name == "test_api"

    def test_cb_service_property(self, policy, mock_cb_service):
        """The ``cb_service`` property is the injected service instance."""
        assert policy.cb_service is mock_cb_service

    def test_success_result_has_circuit_breaker_in_executed_policies(self, policy):
        """A success result's executed_policies contains 'circuit_breaker'."""
        result = policy.execute(lambda: "ok")
        assert "circuit_breaker" in result.executed_policies

    def test_rejected_result_has_circuit_breaker_in_executed_policies(
        self, mock_cb_service
    ):
        """A rejection result's executed_policies contains 'circuit_breaker'."""
        mock_cb_service.should_allow.return_value = False
        mock_cb_service.should_allow_with_state.return_value = _reject_decision()
        policy = CircuitBreakerPolicy(
            service_name="test_api", cb_service=mock_cb_service
        )
        result = policy.execute(lambda: "ok")
        assert "circuit_breaker" in result.executed_policies

    def test_disabled_result_has_circuit_breaker_in_executed_policies(
        self, disabled_cb_service
    ):
        """A disabled-path result's executed_policies contains 'circuit_breaker'."""
        policy = CircuitBreakerPolicy(
            service_name="test_api", cb_service=disabled_cb_service
        )
        result = policy.execute(lambda: "ok")
        assert "circuit_breaker" in result.executed_policies

    def test_success_outcome_is_success(self, policy):
        """On success the outcome is PolicyOutcome.SUCCESS."""
        result = policy.execute(lambda: 42)
        assert result.outcome == PolicyOutcome.SUCCESS

    def test_rejected_outcome_is_rejected(self, mock_cb_service):
        """On rejection the outcome is PolicyOutcome.REJECTED."""
        mock_cb_service.should_allow.return_value = False
        mock_cb_service.should_allow_with_state.return_value = _reject_decision()
        policy = CircuitBreakerPolicy(
            service_name="test_api", cb_service=mock_cb_service
        )
        result = policy.execute(lambda: 42)
        assert result.outcome == PolicyOutcome.REJECTED

    def test_default_failure_exceptions(self):
        """failure_exceptions defaults to (Exception,)."""
        policy = CircuitBreakerPolicy(
            service_name="test",
            cb_service=MagicMock(
                is_enabled=True, should_allow=MagicMock(return_value=True)
            ),
        )
        assert policy._failure_exceptions == (Exception,)

    def test_default_ignore_exceptions(self):
        """ignore_exceptions defaults to an empty tuple."""
        policy = CircuitBreakerPolicy(
            service_name="test",
            cb_service=MagicMock(
                is_enabled=True, should_allow=MagicMock(return_value=True)
            ),
        )
        assert policy._ignore_exceptions == ()


# =============================================================================
# CB disabled (Behavior)
# =============================================================================


class TestCircuitBreakerPolicyDisabledBehavior:
    """Behaviour with the CB disabled."""

    def test_disabled_cb_executes_function_directly(self, disabled_cb_service):
        """CB disabled → the function runs directly."""
        policy = CircuitBreakerPolicy(
            service_name="test_api", cb_service=disabled_cb_service
        )
        result = policy.execute(lambda: "direct_result")
        assert result.value == "direct_result"

    def test_disabled_cb_returns_success(self, disabled_cb_service):
        """CB disabled → the outcome is SUCCESS."""
        policy = CircuitBreakerPolicy(
            service_name="test_api", cb_service=disabled_cb_service
        )
        result = policy.execute(lambda: 123)
        assert result.outcome == PolicyOutcome.SUCCESS
        assert result.success is True

    def test_disabled_cb_does_not_call_should_allow(self, disabled_cb_service):
        """CB disabled → should_allow() is not called."""
        policy = CircuitBreakerPolicy(
            service_name="test_api", cb_service=disabled_cb_service
        )
        policy.execute(lambda: "ok")
        disabled_cb_service.should_allow.assert_not_called()

    def test_disabled_cb_does_not_call_record_success(self, disabled_cb_service):
        """CB disabled → record_success() is not called."""
        policy = CircuitBreakerPolicy(
            service_name="test_api", cb_service=disabled_cb_service
        )
        policy.execute(lambda: "ok")
        disabled_cb_service.record_success.assert_not_called()

    def test_disabled_cb_passes_args_and_kwargs(self, disabled_cb_service):
        """CB disabled → args and kwargs reach the function."""
        policy = CircuitBreakerPolicy(
            service_name="test_api", cb_service=disabled_cb_service
        )

        def func(a, b, key=None):
            return (a, b, key)

        result = policy.execute(func, 1, 2, key="val")
        assert result.value == (1, 2, "val")


# =============================================================================
# CB OPEN — rejection (Behavior)
# =============================================================================


class TestCircuitBreakerPolicyRejectedBehavior:
    """Rejection behaviour while the CB is OPEN."""

    def test_rejected_when_should_allow_false(self, mock_cb_service):
        """should_allow() == False → REJECTED is returned."""
        mock_cb_service.should_allow.return_value = False
        mock_cb_service.should_allow_with_state.return_value = _reject_decision()
        policy = CircuitBreakerPolicy(
            service_name="test_api", cb_service=mock_cb_service
        )
        result = policy.execute(lambda: "should_not_run")
        assert result.rejected is True

    def test_rejected_error_is_circuit_breaker_open_error(self, mock_cb_service):
        """On rejection ``error`` is a CircuitBreakerOpenError."""
        mock_cb_service.should_allow.return_value = False
        mock_cb_service.should_allow_with_state.return_value = _reject_decision()
        policy = CircuitBreakerPolicy(
            service_name="test_api", cb_service=mock_cb_service
        )
        result = policy.execute(lambda: "nope")
        assert isinstance(result.error, CircuitBreakerOpenError)

    def test_rejected_error_has_service_name(self, mock_cb_service):
        """On rejection ``error.service_name`` equals the policy's service_name."""
        mock_cb_service.should_allow.return_value = False
        mock_cb_service.should_allow_with_state.return_value = _reject_decision()
        policy = CircuitBreakerPolicy(
            service_name="payment_api", cb_service=mock_cb_service
        )
        result = policy.execute(lambda: "nope")
        assert result.error.service_name == "payment_api"

    def test_rejected_metadata_contains_service_name(self, mock_cb_service):
        """On rejection the metadata carries service_name."""
        mock_cb_service.should_allow.return_value = False
        mock_cb_service.should_allow_with_state.return_value = _reject_decision()
        mock_cb_service.get_state.return_value = "open"
        policy = CircuitBreakerPolicy(
            service_name="test_api", cb_service=mock_cb_service
        )
        result = policy.execute(lambda: "nope")
        assert result.metadata["service_name"] == "test_api"

    def test_rejected_metadata_contains_state(self, mock_cb_service):
        """On rejection the metadata carries the state."""
        mock_cb_service.should_allow.return_value = False
        mock_cb_service.should_allow_with_state.return_value = _reject_decision()
        mock_cb_service.get_state.return_value = "open"
        policy = CircuitBreakerPolicy(
            service_name="test_api", cb_service=mock_cb_service
        )
        result = policy.execute(lambda: "nope")
        assert result.metadata["state"] == "open"

    def test_rejected_does_not_execute_function(self, mock_cb_service):
        """On rejection ``func`` does not run."""
        mock_cb_service.should_allow.return_value = False
        mock_cb_service.should_allow_with_state.return_value = _reject_decision()
        policy = CircuitBreakerPolicy(
            service_name="test_api", cb_service=mock_cb_service
        )
        func = MagicMock()
        policy.execute(func)
        func.assert_not_called()

    def test_rejected_value_is_none(self, mock_cb_service):
        """On rejection ``value`` is None."""
        mock_cb_service.should_allow.return_value = False
        mock_cb_service.should_allow_with_state.return_value = _reject_decision()
        policy = CircuitBreakerPolicy(
            service_name="test_api", cb_service=mock_cb_service
        )
        result = policy.execute(lambda: "nope")
        assert result.value is None


# =============================================================================
# Success path (Behavior)
# =============================================================================


class TestCircuitBreakerPolicySuccessBehavior:
    """The success path."""

    def test_success_returns_function_value(self, policy):
        """On success ``func``'s return value lands in result.value."""
        result = policy.execute(lambda: "success_value")
        assert result.value == "success_value"

    def test_success_calls_record_success(self, policy, mock_cb_service):
        """On success record_success(service_name, hint_state=, hint_epoch=) is called (490 D4)."""
        policy.execute(lambda: "ok")
        decision = mock_cb_service.should_allow_with_state.return_value
        mock_cb_service.record_success.assert_called_once_with(
            "test_api",
            hint_state=decision.state,
            hint_epoch=decision.window_epoch,
        )

    def test_success_does_not_call_record_failure(self, policy, mock_cb_service):
        """On success record_failure() is not called."""
        policy.execute(lambda: "ok")
        mock_cb_service.record_failure.assert_not_called()

    def test_success_calls_should_allow_with_service_name(
        self, policy, mock_cb_service
    ):
        """should_allow_with_state() receives the service_name (#485 D2)."""
        policy.execute(lambda: "ok")
        mock_cb_service.should_allow_with_state.assert_called_once_with("test_api")

    def test_success_passes_args_to_function(self, policy):
        """args reach the function exactly."""

        def add(a, b):
            return a + b

        result = policy.execute(add, 3, 7)
        assert result.value == 10

    def test_success_passes_kwargs_to_function(self, policy):
        """kwargs reach the function exactly."""

        def greet(name, prefix="Hello"):
            return f"{prefix}, {name}"

        result = policy.execute(greet, "world", prefix="Hi")
        assert result.value == "Hi, world"

    def test_success_result_is_success_property_true(self, policy):
        """A success result's ``.success`` property is True."""
        result = policy.execute(lambda: "ok")
        assert result.success is True

    def test_success_result_rejected_property_false(self, policy):
        """A success result's ``.rejected`` property is False."""
        result = policy.execute(lambda: "ok")
        assert result.rejected is False


# =============================================================================
# Failure path (Behavior)
# =============================================================================


class TestCircuitBreakerPolicyFailureBehavior:
    """The failure path."""

    def test_failure_calls_record_failure_with_error_context(
        self, policy, mock_cb_service
    ):
        """On failure record_failure(service_name, error_context=..., hint_state=...) is called (490 D4)."""
        with pytest.raises(ValueError):
            policy.execute(self._raise_value_error)
        decision = mock_cb_service.should_allow_with_state.return_value
        mock_cb_service.record_failure.assert_called_once_with(
            "test_api",
            error_context={"error": "bad value", "type": "ValueError"},
            hint_state=decision.state,
        )

    def test_failure_reraises_exception(self, policy):
        """On failure the exception re-raises to the caller."""
        with pytest.raises(ValueError, match="bad value"):
            policy.execute(self._raise_value_error)

    def test_failure_does_not_call_record_success(self, policy, mock_cb_service):
        """On failure record_success() is not called."""
        with pytest.raises(ValueError):
            policy.execute(self._raise_value_error)
        mock_cb_service.record_success.assert_not_called()

    def test_failure_error_context_type_field(self, policy, mock_cb_service):
        """error_context's ``type`` field is the exception class name."""
        with pytest.raises(RuntimeError):
            policy.execute(self._raise_runtime_error)
        call_args = mock_cb_service.record_failure.call_args
        assert call_args[1]["error_context"]["type"] == "RuntimeError"

    def test_failure_error_context_error_field(self, policy, mock_cb_service):
        """error_context's ``error`` field is str(e)."""
        with pytest.raises(RuntimeError):
            policy.execute(self._raise_runtime_error)
        call_args = mock_cb_service.record_failure.call_args
        assert call_args[1]["error_context"]["error"] == "runtime fail"

    @staticmethod
    def _raise_value_error():
        raise ValueError("bad value")

    @staticmethod
    def _raise_runtime_error():
        raise RuntimeError("runtime fail")


# =============================================================================
# Exception filtering (Behavior) — _is_failure()
# =============================================================================


class TestCircuitBreakerPolicyExceptionFilterBehavior:
    """Exception filtering — §7.2."""

    def test_ignore_exceptions_skips_record_failure(self, mock_cb_service):
        """An exception matching ignore_exceptions does not call record_failure()."""
        policy = CircuitBreakerPolicy(
            service_name="test_api",
            cb_service=mock_cb_service,
            ignore_exceptions=(ValueError,),
        )
        with pytest.raises(ValueError):
            policy.execute(lambda: (_ for _ in ()).throw(ValueError("ignored")))
        mock_cb_service.record_failure.assert_not_called()

    def test_ignore_exceptions_still_reraises(self, mock_cb_service):
        """An ignored exception still re-raises to the caller."""
        policy = CircuitBreakerPolicy(
            service_name="test_api",
            cb_service=mock_cb_service,
            ignore_exceptions=(ValueError,),
        )
        with pytest.raises(ValueError, match="ignored"):
            policy.execute(lambda: (_ for _ in ()).throw(ValueError("ignored")))

    def test_failure_exceptions_only_counts_specified_types(self, mock_cb_service):
        """failure_exceptions=(ValueError,) → only ValueError calls record_failure."""
        policy = CircuitBreakerPolicy(
            service_name="test_api",
            cb_service=mock_cb_service,
            failure_exceptions=(ValueError,),
        )
        # ValueError → record_failure is called
        with pytest.raises(ValueError):
            policy.execute(lambda: (_ for _ in ()).throw(ValueError("counted")))
        assert mock_cb_service.record_failure.call_count == 1

    def test_failure_exceptions_ignores_non_matching_types(self, mock_cb_service):
        """failure_exceptions=(ValueError,) → RuntimeError does not call record_failure."""
        policy = CircuitBreakerPolicy(
            service_name="test_api",
            cb_service=mock_cb_service,
            failure_exceptions=(ValueError,),
        )
        with pytest.raises(RuntimeError):
            policy.execute(lambda: (_ for _ in ()).throw(RuntimeError("not counted")))
        mock_cb_service.record_failure.assert_not_called()

    def test_ignore_takes_precedence_over_failure(self, mock_cb_service):
        """ignore_exceptions outranks failure_exceptions."""
        policy = CircuitBreakerPolicy(
            service_name="test_api",
            cb_service=mock_cb_service,
            failure_exceptions=(Exception,),
            ignore_exceptions=(ValueError,),
        )
        with pytest.raises(ValueError):
            policy.execute(lambda: (_ for _ in ()).throw(ValueError("both")))
        mock_cb_service.record_failure.assert_not_called()

    def test_is_failure_with_subclass(self, mock_cb_service):
        """A subclass of a failure_exceptions entry counts as a failure too."""
        policy = CircuitBreakerPolicy(
            service_name="test_api",
            cb_service=mock_cb_service,
            failure_exceptions=(OSError,),
        )
        with pytest.raises(ConnectionError):  # ConnectionError subclasses OSError
            policy.execute(lambda: (_ for _ in ()).throw(ConnectionError("subclass")))
        assert mock_cb_service.record_failure.call_count == 1

    def test_is_failure_method_directly(self):
        """_is_failure() checked directly."""
        policy = CircuitBreakerPolicy(
            service_name="test",
            cb_service=MagicMock(),
            failure_exceptions=(ValueError, TypeError),
            ignore_exceptions=(KeyError,),
        )
        assert policy._is_failure(ValueError("v")) is True
        assert policy._is_failure(TypeError("t")) is True
        assert policy._is_failure(KeyError("k")) is False
        assert policy._is_failure(RuntimeError("r")) is False


# =============================================================================
# PolicyContext pass-through (Behavior)
# =============================================================================


class TestCircuitBreakerPolicyContextBehavior:
    """PolicyContext pass-through."""

    def test_execute_accepts_context_parameter(self, policy):
        """execute() accepts a ``context`` parameter."""
        ctx = PolicyContext(order_id="order-123", trace_id="trace-abc")
        result = policy.execute(lambda: "with_context", context=ctx)
        assert result.value == "with_context"
        assert result.outcome == PolicyOutcome.SUCCESS

    def test_execute_works_without_context(self, policy):
        """context=None (the default) works too."""
        result = policy.execute(lambda: "no_context")
        assert result.value == "no_context"
        assert result.outcome == PolicyOutcome.SUCCESS


# =============================================================================
# CircuitBreakerOpenError (Contract)
# =============================================================================


class TestCircuitBreakerOpenErrorContract:
    """The CircuitBreakerOpenError exception contract — exceptions.py."""

    def test_service_name_attribute(self):
        """The ``service_name`` attribute is set."""
        error = CircuitBreakerOpenError("payment_api")
        assert error.service_name == "payment_api"

    def test_default_message_format(self):
        """The default message format."""
        error = CircuitBreakerOpenError("payment_api")
        assert str(error) == "Circuit breaker 'payment_api' is OPEN"

    def test_custom_message(self):
        """A caller-supplied message."""
        error = CircuitBreakerOpenError("api", message="custom msg")
        assert str(error) == "custom msg"

    def test_inherits_from_exception(self):
        """It inherits Exception."""
        error = CircuitBreakerOpenError("test")
        assert isinstance(error, Exception)

    def test_is_not_base_exception(self):
        """Exception lineage — an ordinary Exception, not a bare BaseException."""
        from baldur.core.exceptions import BaldurError, CircuitBreakerError

        error = CircuitBreakerOpenError("test")
        assert isinstance(error, Exception)
        assert isinstance(error, CircuitBreakerError)
        assert isinstance(error, BaldurError)


# =============================================================================
# circuit_breaker decorator (Behavior)
# =============================================================================


class TestCircuitBreakerDecoratorBehavior:
    """The @circuit_breaker() decorator."""

    def test_decorator_wraps_function_with_policy(self):
        """Decorating attaches a ``.policy`` attribute to the wrapper."""
        mock_service = MagicMock()
        mock_service.is_enabled = True
        mock_service.should_allow.return_value = True
        mock_service.record_success.return_value = None

        @circuit_breaker("test_api", cb_service=mock_service)
        def my_func():
            return "hello"

        assert hasattr(my_func, "policy")
        assert isinstance(my_func.policy, CircuitBreakerPolicy)

    def test_decorator_preserves_function_name(self):
        """The decorator preserves the original function name (@wraps)."""
        mock_service = MagicMock()
        mock_service.is_enabled = True
        mock_service.should_allow.return_value = True

        @circuit_breaker("test_api", cb_service=mock_service)
        def original_function():
            """Original docstring."""
            return "ok"

        assert original_function.__name__ == "original_function"
        assert original_function.__doc__ == "Original docstring."

    def test_decorator_uses_qualname_when_service_name_none(self):
        """service_name=None → func.__qualname__ is the default."""
        mock_service = MagicMock()
        mock_service.is_enabled = True
        mock_service.should_allow.return_value = True
        mock_service.record_success.return_value = None

        @circuit_breaker(service_name=None, cb_service=mock_service)
        def my_special_func():
            return "ok"

        # Defined inside the test class, so __qualname__ is ClassName.function
        expected_qualname = (
            my_special_func.__wrapped__.__qualname__
            if hasattr(my_special_func, "__wrapped__")
            else "my_special_func"
        )
        assert my_special_func.policy.service_name == expected_qualname

    def test_decorator_returns_policy_result(self):
        """Calling a decorated function returns a PolicyResult."""
        mock_service = MagicMock()
        mock_service.is_enabled = True
        mock_service.should_allow.return_value = True
        mock_service.record_success.return_value = None

        @circuit_breaker("test_api", cb_service=mock_service)
        def my_func():
            return 42

        result = my_func()
        assert isinstance(result, PolicyResult)
        assert result.value == 42
        assert result.outcome == PolicyOutcome.SUCCESS

    def test_decorator_explicit_service_name(self):
        """An explicit service_name reaches the policy."""
        mock_service = MagicMock()
        mock_service.is_enabled = True
        mock_service.should_allow.return_value = True
        mock_service.record_success.return_value = None

        @circuit_breaker("payment_api", cb_service=mock_service)
        def pay():
            return "paid"

        assert pay.policy.service_name == "payment_api"

    def test_decorator_passes_failure_exceptions(self):
        """failure_exceptions reaches the policy."""
        mock_service = MagicMock()
        mock_service.is_enabled = True

        @circuit_breaker(
            "test_api",
            cb_service=mock_service,
            failure_exceptions=(ValueError, TypeError),
        )
        def func():
            return "ok"

        assert func.policy._failure_exceptions == (ValueError, TypeError)

    def test_decorator_passes_ignore_exceptions(self):
        """ignore_exceptions reaches the policy."""
        mock_service = MagicMock()
        mock_service.is_enabled = True

        @circuit_breaker(
            "test_api",
            cb_service=mock_service,
            ignore_exceptions=(KeyError,),
        )
        def func():
            return "ok"

        assert func.policy._ignore_exceptions == (KeyError,)

    def test_decorator_rejected_when_cb_open(self):
        """With the CB OPEN the decorated function returns REJECTED too."""
        mock_service = MagicMock()
        mock_service.is_enabled = True
        mock_service.should_allow.return_value = False
        mock_service.should_allow_with_state.return_value = _reject_decision()
        mock_service.get_state.return_value = "open"

        @circuit_breaker("test_api", cb_service=mock_service)
        def func():
            return "should_not_run"

        result = func()
        assert result.rejected is True
        assert isinstance(result.error, CircuitBreakerOpenError)


# =============================================================================
# Behavior — sync helper extraction parity (670 D2 / R1)
# =============================================================================


class TestCircuitBreakerHelperExtractionBehavior:
    """The extracted ``_admit`` / ``_direct_result`` / ``_on_success`` /
    ``_on_failure`` helpers preserve the sync state machine (behavior-preserving
    R1 refactor) and are the single source of truth the async wrapper reuses.

    The helpers take the breaker service ``execute`` resolved once for the
    call; these direct calls hand them the policy's own binding.
    """

    def test_admit_returns_direct_verdict_when_disabled(self, disabled_cb_service):
        """CB disabled → ``_admit`` verdict is 'direct' (run once, never record)."""
        policy = CircuitBreakerPolicy(
            service_name="test_api",
            cb_service=disabled_cb_service,
        )

        verdict, reject_result, hint_state = policy._admit(policy.cb_service)

        assert verdict == "direct"
        assert reject_result is None
        assert hint_state is None

    def test_admit_returns_run_verdict_with_the_decision_when_allowed(
        self, policy, mock_cb_service
    ):
        """Admitted → verdict 'run' and the third element is the whole decision."""
        decision = mock_cb_service.should_allow_with_state.return_value

        verdict, reject_result, admitted = policy._admit(policy.cb_service)

        assert verdict == "run"
        assert reject_result is None
        # 490 D4: the loaded state is threaded through so record_* skips a
        # refetch; the decision also carries the window epoch the fast path
        # is guarded by, so the whole object travels.
        assert admitted is decision
        assert admitted.state is decision.state

    def test_admit_returns_reject_verdict_when_open(self, mock_cb_service):
        """CB OPEN → verdict 'reject' with a REJECTED CircuitBreakerOpenError result."""
        mock_cb_service.should_allow_with_state.return_value = _reject_decision()
        policy = CircuitBreakerPolicy(
            service_name="test_api",
            cb_service=mock_cb_service,
        )

        verdict, reject_result, hint_state = policy._admit(policy.cb_service)

        assert verdict == "reject"
        assert reject_result.outcome == PolicyOutcome.REJECTED
        assert isinstance(reject_result.error, CircuitBreakerOpenError)
        assert hint_state is None

    def test_direct_result_builds_success_without_recording(
        self, policy, mock_cb_service
    ):
        """``_direct_result`` wraps the value in SUCCESS and records nothing."""
        result = policy._direct_result("value")

        assert result.outcome == PolicyOutcome.SUCCESS
        assert result.value == "value"
        assert result.executed_policies == ["circuit_breaker"]
        mock_cb_service.record_success.assert_not_called()
        mock_cb_service.record_failure.assert_not_called()

    def test_on_success_records_with_the_decisions_hint_and_epoch(
        self, policy, mock_cb_service
    ):
        """``_on_success`` forwards the decision's state and epoch to ``record_success``."""
        decision = MagicMock(spec=CircuitBreakerDecision)

        result = policy._on_success(
            "value", decision, scope=None, service=policy.cb_service
        )

        assert result.outcome == PolicyOutcome.SUCCESS
        mock_cb_service.record_success.assert_called_once_with(
            "test_api",
            hint_state=decision.state,
            hint_epoch=decision.window_epoch,
        )

    def test_on_failure_records_when_counted_as_failure(self, policy, mock_cb_service):
        """``_on_failure`` records a counted failure (default failure_exceptions)."""
        decision = MagicMock(spec=CircuitBreakerDecision)
        error = RuntimeError("boom")

        policy._on_failure(error, decision, scope=None, service=policy.cb_service)

        mock_cb_service.record_failure.assert_called_once()
        assert (
            mock_cb_service.record_failure.call_args.kwargs["hint_state"]
            is decision.state
        )

    def test_on_failure_skips_record_for_ignored_exception(self, mock_cb_service):
        """``_on_failure`` skips ``record_failure`` for an ignored exception type."""
        policy = CircuitBreakerPolicy(
            service_name="test_api",
            cb_service=mock_cb_service,
            ignore_exceptions=(KeyError,),
        )

        policy._on_failure(
            KeyError("ignored"), None, scope=None, service=policy.cb_service
        )

        mock_cb_service.record_failure.assert_not_called()

    def test_closed_success_does_zero_extra_state_acquire(
        self, policy, mock_cb_service
    ):
        """R1: a CLOSED steady-state success does NOT re-acquire state.

        ``should_allow_with_state`` is the single fetch; ``record_success``
        reuses its hint_state, so neither ``get_or_create_state`` nor the legacy
        ``get_state`` lookup runs on the hot path.
        """
        result = policy.execute(lambda: "ok")

        assert result.outcome == PolicyOutcome.SUCCESS
        mock_cb_service.should_allow_with_state.assert_called_once_with("test_api")
        mock_cb_service.get_or_create_state.assert_not_called()
        mock_cb_service.get_state.assert_not_called()


# =============================================================================
# Behavior — @circuit_breaker async dual-dispatch (670 D5 / G7)
# =============================================================================


def _real_low_threshold_service(failure_threshold: int = 2):
    """A real CircuitBreakerService (InMemory) that opens after ``failure_threshold``."""
    from baldur.adapters.memory.circuit_breaker import (
        InMemoryCircuitBreakerStateRepository,
    )
    from baldur.services.circuit_breaker.config import CircuitBreakerConfig
    from baldur.services.circuit_breaker.service import CircuitBreakerService

    config = CircuitBreakerConfig(
        enabled=True,
        failure_threshold=failure_threshold,
        minimum_calls=1,
        failure_rate_threshold=0,
        recovery_timeout=60,
    )
    return CircuitBreakerService(
        config=config,
        repository=InMemoryCircuitBreakerStateRepository(),
    )


class TestCircuitBreakerDecoratorAsyncBehavior:
    """``@circuit_breaker`` on an ``async def`` protects it (no silent bypass)."""

    def test_decorator_async_returns_policy_result(self):
        """An async-decorated function, when awaited, returns a PolicyResult."""
        service = MagicMock()
        service.is_enabled = True
        service.should_allow_with_state.return_value = CircuitBreakerDecision(
            allowed=True, state=MagicMock(state="closed")
        )
        service.record_success.return_value = None

        @circuit_breaker("async_api", cb_service=service)
        async def call_async():
            return 42

        result = asyncio.run(call_async())

        assert isinstance(result, PolicyResult)
        assert result.value == 42
        assert result.outcome == PolicyOutcome.SUCCESS

    def test_decorator_async_wrapper_exposes_sync_policy(self):
        """``wrapper.policy`` exposes the underlying sync CircuitBreakerPolicy."""
        service = MagicMock()
        service.is_enabled = True

        @circuit_breaker("async_api", cb_service=service)
        async def call_async():
            return "ok"

        assert isinstance(call_async.policy, CircuitBreakerPolicy)
        assert call_async.policy.cb_service is service

    def test_decorator_async_records_awaited_success(self):
        """The awaited outcome (not coroutine creation) drives ``record_success``."""
        service = MagicMock()
        service.is_enabled = True
        service.should_allow_with_state.return_value = CircuitBreakerDecision(
            allowed=True, state=MagicMock(state="closed")
        )
        service.record_success.return_value = None

        @circuit_breaker("async_api", cb_service=service)
        async def call_async():
            return "done"

        asyncio.run(call_async())

        service.record_success.assert_called_once()
        service.record_failure.assert_not_called()

    def test_decorator_async_records_awaited_failure_and_reraises(self):
        """An awaited failure records a real failure and re-raises (no false success)."""
        service = MagicMock()
        service.is_enabled = True
        service.should_allow_with_state.return_value = CircuitBreakerDecision(
            allowed=True, state=MagicMock(state="closed")
        )
        service.record_failure.return_value = None

        @circuit_breaker("async_api", cb_service=service)
        async def call_async():
            raise RuntimeError("async boom")

        with pytest.raises(RuntimeError, match="async boom"):
            asyncio.run(call_async())

        service.record_failure.assert_called_once()
        service.record_success.assert_not_called()

    def test_decorator_async_preserves_function_name(self):
        """functools.wraps preserves the async function's name and docstring."""
        service = MagicMock()
        service.is_enabled = True

        @circuit_breaker("async_api", cb_service=service)
        async def original_async():
            """Async docstring."""
            return "ok"

        assert original_async.__name__ == "original_async"
        assert original_async.__doc__ == "Async docstring."

    def test_decorator_async_opens_after_threshold_failures(self):
        """Accumulated awaited failures open the breaker → later calls REJECTED.

        The G7 fix: before 670 the sync wrapper wrapped an un-awaited coroutine
        and recorded a false success, so the breaker never opened on an async
        target. Here real awaited failures accumulate and trip it.
        """
        from baldur.services.circuit_breaker.config import CircuitState

        service = _real_low_threshold_service(failure_threshold=2)

        @circuit_breaker("async_open_api", cb_service=service)
        async def boom():
            raise RuntimeError("dependency down")

        rejected_seen = False
        for _ in range(5):
            try:
                result = asyncio.run(boom())
                if result.rejected:
                    rejected_seen = True
            except RuntimeError:
                pass

        assert rejected_seen is True
        assert service.get_or_create_state("async_open_api").state == CircuitState.OPEN
