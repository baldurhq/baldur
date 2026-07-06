"""AsyncCircuitBreakerPolicy unit tests (670 D2).

Target:
- services/circuit_breaker/policy.py (AsyncCircuitBreakerPolicy)

AsyncCircuitBreakerPolicy composes a synchronous CircuitBreakerPolicy and drives
its shared ``_admit`` / ``_direct_result`` / ``_on_success`` / ``_on_failure``
helpers, awaiting ``func`` in place of calling it. The state machine is a single
source of truth across sync and async, so these tests assert that the async
wrapper reproduces the sync admission/record semantics and preserves the
load-bearing ``except Exception`` boundary that lets ``CancelledError`` escape
uncounted.

UNIT_TEST_GUIDELINES.md:
- Contract: hardcoded fixed identifiers / result structure (name, outcome).
- Behavior: source-referenced record_success / record_failure interactions.
- Fixtures: single-file scope → in-file (§5.1).
- Mock: MagicMock(spec-free) CircuitBreakerService injected via constructor.

Techniques (§8):
- §8.7 state transition — CLOSED record, OPEN → REJECTED, disabled/observe direct.
- §8.5 dependency interaction — record_success / record_failure call counts.
- §8.2 exception/edge — CancelledError escapes the ``except Exception`` gate.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from baldur.core.execution_mode import (
    ExecutionMode,
    clear_execution_mode_override,
    set_execution_mode,
)
from baldur.interfaces.resilience_policy import (
    AsyncResiliencePolicy,
    PolicyOutcome,
    PolicyResult,
)
from baldur.services.circuit_breaker.config import (
    CircuitBreakerDecision,
    CircuitState,
)
from baldur.services.circuit_breaker.exceptions import CircuitBreakerOpenError
from baldur.services.circuit_breaker.policy import (
    AsyncCircuitBreakerPolicy,
    CircuitBreakerPolicy,
)

# =============================================================================
# Fixtures — single-file scope (§5.1)
# =============================================================================


def _closed_admit_decision() -> CircuitBreakerDecision:
    """A CLOSED admit decision (allowed=True) for should_allow_with_state."""
    return CircuitBreakerDecision(allowed=True, state=MagicMock(state="closed"))


def _open_reject_decision() -> CircuitBreakerDecision:
    """An OPEN reject decision (allowed=False) for should_allow_with_state."""
    return CircuitBreakerDecision(allowed=False, state=MagicMock(state="open"))


@pytest.fixture
def mock_cb_service():
    """CircuitBreakerService Mock — enabled + CLOSED admit by default."""
    service = MagicMock()
    service.is_enabled = True
    service.should_allow_with_state.return_value = _closed_admit_decision()
    service.record_success.return_value = None
    service.record_failure.return_value = None
    return service


@pytest.fixture
def async_policy(mock_cb_service):
    """AsyncCircuitBreakerPolicy composing a sync policy over the mock service."""
    inner = CircuitBreakerPolicy(
        service_name="async_api",
        cb_service=mock_cb_service,
        hooks=[],
    )
    return AsyncCircuitBreakerPolicy(inner)


# =============================================================================
# Contract — identifiers / composition / protocol
# =============================================================================


class TestAsyncCircuitBreakerPolicyContract:
    """Fixed identifiers, exposed composition, and Protocol conformance."""

    def test_name_is_circuit_breaker(self, async_policy):
        """name property is 'circuit_breaker' (matches the sync sibling)."""
        assert async_policy.name == "circuit_breaker"

    def test_service_name_delegates_to_inner(self, async_policy):
        """service_name reflects the composed sync policy's service_name."""
        assert async_policy.service_name == "async_api"

    def test_policy_property_exposes_inner_sync_policy(
        self, async_policy, mock_cb_service
    ):
        """.policy returns the composed CircuitBreakerPolicy (state-machine owner)."""
        assert isinstance(async_policy.policy, CircuitBreakerPolicy)
        assert async_policy.policy.service_name == "async_api"

    def test_cb_service_shared_with_inner_policy(self, async_policy, mock_cb_service):
        """.cb_service is the SAME instance the composed sync policy holds."""
        assert async_policy.cb_service is mock_cb_service
        assert async_policy.cb_service is async_policy.policy.cb_service

    def test_satisfies_async_resilience_protocol(self, async_policy):
        """AsyncCircuitBreakerPolicy is an AsyncResiliencePolicy."""
        assert isinstance(async_policy, AsyncResiliencePolicy)


# =============================================================================
# Behavior — admission + record state machine (§8.7 / §8.5)
# =============================================================================


class TestAsyncCircuitBreakerBehavior:
    """AsyncCircuitBreakerPolicy.execute() state machine — awaited outcome."""

    @pytest.mark.asyncio
    async def test_closed_success_records_success_and_returns_value(
        self, async_policy, mock_cb_service
    ):
        """CLOSED + coroutine succeeds → record_success called, SUCCESS + value."""

        async def ok():
            return "async-ok"

        result = await async_policy.execute(ok)

        assert isinstance(result, PolicyResult)
        assert result.outcome == PolicyOutcome.SUCCESS
        assert result.value == "async-ok"
        mock_cb_service.record_success.assert_called_once()
        mock_cb_service.record_failure.assert_not_called()

    @pytest.mark.asyncio
    async def test_closed_failure_records_failure_and_reraises(
        self, async_policy, mock_cb_service
    ):
        """CLOSED + coroutine raises → record_failure called, exception re-raised."""

        async def boom():
            raise RuntimeError("async-boom")

        with pytest.raises(RuntimeError, match="async-boom"):
            await async_policy.execute(boom)

        mock_cb_service.record_failure.assert_called_once()
        mock_cb_service.record_success.assert_not_called()

    @pytest.mark.asyncio
    async def test_open_state_rejects_without_running_func(self, mock_cb_service):
        """OPEN → REJECTED PolicyResult (CircuitBreakerOpenError); func not awaited."""
        mock_cb_service.should_allow_with_state.return_value = _open_reject_decision()
        inner = CircuitBreakerPolicy(
            service_name="async_api",
            cb_service=mock_cb_service,
            hooks=[],
        )
        policy = AsyncCircuitBreakerPolicy(inner)

        ran = {"n": 0}

        async def guarded():
            ran["n"] += 1
            return "should-not-run"

        result = await policy.execute(guarded)

        assert result.outcome == PolicyOutcome.REJECTED
        assert isinstance(result.error, CircuitBreakerOpenError)
        assert ran["n"] == 0
        mock_cb_service.record_success.assert_not_called()
        mock_cb_service.record_failure.assert_not_called()

    @pytest.mark.asyncio
    async def test_disabled_runs_directly_without_recording(self):
        """CB disabled → run func once, SUCCESS, and never touch record_*."""
        service = MagicMock()
        service.is_enabled = False
        inner = CircuitBreakerPolicy(
            service_name="async_api",
            cb_service=service,
            hooks=[],
        )
        policy = AsyncCircuitBreakerPolicy(inner)

        async def ok():
            return 7

        result = await policy.execute(ok)

        assert result.outcome == PolicyOutcome.SUCCESS
        assert result.value == 7
        service.record_success.assert_not_called()
        service.record_failure.assert_not_called()
        # Admission is skipped entirely when disabled.
        service.should_allow_with_state.assert_not_called()

    @pytest.mark.asyncio
    async def test_observe_only_runs_once_without_recording(self, mock_cb_service):
        """Observe-only (shadow) → single direct run, no admission mutation, no record."""
        mock_cb_service.get_or_create_state.return_value = MagicMock(
            state=CircuitState.CLOSED
        )
        inner = CircuitBreakerPolicy(
            service_name="async_api",
            cb_service=mock_cb_service,
            hooks=[],
        )
        policy = AsyncCircuitBreakerPolicy(inner)

        ran = {"n": 0}

        async def work():
            ran["n"] += 1
            return "observed"

        set_execution_mode(ExecutionMode.shadow())
        try:
            result = await policy.execute(work)
        finally:
            clear_execution_mode_override()

        assert result.outcome == PolicyOutcome.SUCCESS
        assert result.value == "observed"
        assert ran["n"] == 1
        # Observe-only must NOT advance admission or record an outcome.
        mock_cb_service.should_allow_with_state.assert_not_called()
        mock_cb_service.record_success.assert_not_called()
        mock_cb_service.record_failure.assert_not_called()

    @pytest.mark.asyncio
    async def test_cancelled_error_escapes_without_recording_failure(
        self, async_policy, mock_cb_service
    ):
        """A cancelled ``await func()`` propagates CancelledError and records NOTHING.

        CancelledError is a BaseException, so the ``except Exception`` boundary in
        ``execute`` lets it escape untouched — a client-disconnect cancellation
        must not increment the breaker's failure count.
        """

        async def cancelled():
            raise asyncio.CancelledError()

        with pytest.raises(asyncio.CancelledError):
            await async_policy.execute(cancelled)

        mock_cb_service.record_failure.assert_not_called()
        mock_cb_service.record_success.assert_not_called()

    @pytest.mark.asyncio
    async def test_args_and_kwargs_forwarded_to_awaited_func(self, async_policy):
        """Positional/keyword args are forwarded to the awaited coroutine."""
        received = {}

        async def capture(a, b, key=None):
            received["a"] = a
            received["b"] = b
            received["key"] = key
            return "ok"

        await async_policy.execute(capture, 1, 2, key="val")

        assert received == {"a": 1, "b": 2, "key": "val"}
