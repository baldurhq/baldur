"""Unit tests for AsyncTenacityBridgePolicy (672 D9).

``AsyncResiliencePolicy`` over ``tenacity.AsyncRetrying``. Reuses the sync
bridge's constructor, collaborators, sync callbacks, and result-translation
helpers; only the loop driver (``await retrying(...)``) and the marker handling
differ from the sync bridge.

Verification techniques (UNIT_TEST_GUIDELINES §8):
- Structural conformance — the class satisfies ``AsyncResiliencePolicy`` and its
  ``execute`` is a coroutine function.
- §8.8 State transition — success / retry-then-success / all-failed translate to
  the correct ``PolicyResult`` outcomes.
- §8.5 Dependency interaction — the ``_BRIDGE_EXPLICIT_MARKER`` is set as an
  INSTANCE attribute on the AsyncRetrying, and is NOT injected as an ``__init__``
  kwarg (AsyncRetrying is never Level-1-instrumented and vanilla ``__init__``
  rejects the kwarg), even when Level-1 ``instrument_tenacity()`` is active.
- §8.6 from_sync copy — an async bridge built from a sync bridge carries the same
  stop/wait/retry strategies and collaborators.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest
import tenacity

from baldur.bridges.tenacity.policy import (
    _BRIDGE_EXPLICIT_MARKER,
    AsyncTenacityBridgePolicy,
    TenacityBridgePolicy,
)
from baldur.interfaces.resilience_policy import (
    AsyncResiliencePolicy,
    PolicyOutcome,
    PolicyResult,
)

# =============================================================================
# Contract — AsyncResiliencePolicy conformance
# =============================================================================


class TestAsyncTenacityBridgeConformanceContract:
    """The bridge is a structural ``AsyncResiliencePolicy`` with a coroutine
    ``execute`` and the shared ``tenacity_bridge`` name."""

    def test_conforms_to_async_resilience_policy(self):
        policy = AsyncTenacityBridgePolicy(stop=tenacity.stop_after_attempt(1))
        assert isinstance(policy, AsyncResiliencePolicy)

    def test_execute_is_a_coroutine_function(self):
        policy = AsyncTenacityBridgePolicy(stop=tenacity.stop_after_attempt(1))
        assert asyncio.iscoroutinefunction(policy.execute)

    def test_name_is_tenacity_bridge(self):
        policy = AsyncTenacityBridgePolicy(stop=tenacity.stop_after_attempt(1))
        assert policy.name == "tenacity_bridge"


# =============================================================================
# Behavior — outcome translation under AsyncRetrying
# =============================================================================


class TestAsyncTenacityBridgeExecuteBehavior:
    """``execute`` drives the async loop and translates outcomes to PolicyResult."""

    @pytest.mark.asyncio
    async def test_first_try_success_returns_success_result(self):
        policy: AsyncTenacityBridgePolicy[str] = AsyncTenacityBridgePolicy(
            stop=tenacity.stop_after_attempt(3),
            wait=tenacity.wait_fixed(0),
        )

        async def _fn() -> str:
            return "ok"

        result = await policy.execute(_fn)

        assert isinstance(result, PolicyResult)
        assert result.outcome == PolicyOutcome.SUCCESS
        assert result.value == "ok"
        assert result.executed_policies == ["tenacity_bridge"]

    @pytest.mark.asyncio
    async def test_retries_then_succeeds_reports_success_and_attempt_count(self):
        calls = {"n": 0}

        async def _flaky() -> str:
            calls["n"] += 1
            if calls["n"] < 3:
                raise ConnectionError("transient")
            return "recovered"

        policy: AsyncTenacityBridgePolicy[str] = AsyncTenacityBridgePolicy(
            stop=tenacity.stop_after_attempt(5),
            wait=tenacity.wait_fixed(0),
            retry=tenacity.retry_if_exception_type(ConnectionError),
        )

        result = await policy.execute(_flaky)

        assert result.outcome == PolicyOutcome.SUCCESS
        assert result.value == "recovered"
        assert calls["n"] == 3
        assert result.total_attempts == 3

    @pytest.mark.asyncio
    async def test_all_attempts_failed_returns_failure_result(self):
        async def _always_fail() -> str:
            raise ValueError("nope")

        policy: AsyncTenacityBridgePolicy[str] = AsyncTenacityBridgePolicy(
            stop=tenacity.stop_after_attempt(2),
            wait=tenacity.wait_fixed(0),
        )

        result = await policy.execute(_always_fail)

        assert result.outcome == PolicyOutcome.FAILURE
        assert result.success is False

    @pytest.mark.asyncio
    async def test_execute_forwards_args_and_kwargs_to_fn(self):
        async def _echo(a, b, *, c):
            return (a, b, c)

        policy: AsyncTenacityBridgePolicy = AsyncTenacityBridgePolicy(
            stop=tenacity.stop_after_attempt(1),
            wait=tenacity.wait_fixed(0),
        )

        result = await policy.execute(_echo, 1, 2, c=3)

        assert result.value == (1, 2, 3)


# =============================================================================
# Behavior — marker: instance attribute set, kwarg NOT injected (672 D9)
# =============================================================================


class TestAsyncTenacityBridgeMarkerBehavior:
    """The bridge marks the AsyncRetrying via an instance attribute only — it
    never injects the ``_BRIDGE_EXPLICIT_MARKER`` kwarg (vanilla
    ``AsyncRetrying.__init__`` would reject it), even under Level-1 instrument."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "level1_active", [False, True], ids=["no_instrument", "instrumented"]
    )
    async def test_marker_set_as_instance_attr_not_kwarg(
        self, monkeypatch, level1_active
    ):
        if level1_active:
            # Level-1 patches Retrying.__init__ only; the async path must remain
            # immune (it never consults is_instrumented()).
            from baldur.bridges.tenacity.instrument import instrument_tenacity

            instrument_tenacity()

        captured: dict = {}
        real_init = tenacity.AsyncRetrying.__init__

        def _spy_init(self, *args, **kwargs):
            captured["kwargs"] = dict(kwargs)
            captured["self"] = self
            real_init(self, *args, **kwargs)

        monkeypatch.setattr(tenacity.AsyncRetrying, "__init__", _spy_init)

        policy: AsyncTenacityBridgePolicy[str] = AsyncTenacityBridgePolicy(
            stop=tenacity.stop_after_attempt(1),
            wait=tenacity.wait_fixed(0),
        )

        async def _fn() -> str:
            return "ok"

        result = await policy.execute(_fn)

        assert result.outcome == PolicyOutcome.SUCCESS
        # The marker was NOT passed to AsyncRetrying.__init__ ...
        assert _BRIDGE_EXPLICIT_MARKER not in captured["kwargs"]
        # ... but IS set as an instance attribute on the constructed retrying.
        assert getattr(captured["self"], _BRIDGE_EXPLICIT_MARKER) is True


# =============================================================================
# Behavior — from_sync copies strategies + collaborators (672 D9)
# =============================================================================


class TestAsyncTenacityBridgeFromSyncBehavior:
    """``from_sync`` builds an async bridge off a user-built sync bridge so one
    object works on either path."""

    def test_from_sync_returns_async_bridge_copying_strategies(self):
        stop = tenacity.stop_after_attempt(4)
        wait = tenacity.wait_fixed(0)
        retry = tenacity.retry_if_exception_type(ConnectionError)
        sync_bridge: TenacityBridgePolicy = TenacityBridgePolicy(
            stop=stop, wait=wait, retry=retry, domain="payments"
        )

        async_bridge = AsyncTenacityBridgePolicy.from_sync(sync_bridge)

        assert isinstance(async_bridge, AsyncTenacityBridgePolicy)
        assert async_bridge._stop is stop
        assert async_bridge._wait is wait
        assert async_bridge._retry is retry
        assert async_bridge._domain == "payments"

    @pytest.mark.asyncio
    async def test_from_sync_bridge_runs_under_async_loop(self):
        calls = {"n": 0}

        async def _flaky() -> str:
            calls["n"] += 1
            if calls["n"] < 2:
                raise ConnectionError("boom")
            return "done"

        sync_bridge: TenacityBridgePolicy[str] = TenacityBridgePolicy(
            stop=tenacity.stop_after_attempt(3),
            wait=tenacity.wait_fixed(0),
            retry=tenacity.retry_if_exception_type(ConnectionError),
        )

        async_bridge = AsyncTenacityBridgePolicy.from_sync(sync_bridge)
        result = await async_bridge.execute(_flaky)

        assert result.outcome == PolicyOutcome.SUCCESS
        assert result.value == "done"
        assert calls["n"] == 2


# =============================================================================
# Behavior — outbound 429 coordination through the coroutine callback pair
# =============================================================================

_POLICY_TO_THREAD = "baldur.bridges.tenacity.policy.asyncio.to_thread"


class _Throttled(Exception):
    """A 429 the shared classifier recognises."""

    def __init__(self):
        super().__init__("429 too many requests")


def _throttled_response():
    return type("FakeResponse", (), {"status_code": 429})()


def _async_coordinator():
    """A spec'd coordinator whose awaitable wait admits every attempt."""
    from unittest.mock import MagicMock

    from baldur.services.rate_limit_coordinator import RateLimitCoordinator
    from baldur.services.rate_limit_coordinator.models import RateLimitResult

    coordinator = MagicMock(spec=RateLimitCoordinator)
    coordinator.await_if_needed.return_value = RateLimitResult(waited=False)
    coordinator.on_rate_limited.return_value = 0.0
    return coordinator


def _keyed_async_bridge(coordinator, **kwargs) -> AsyncTenacityBridgePolicy:
    kwargs.setdefault("stop", tenacity.stop_after_attempt(3))
    kwargs.setdefault("wait", tenacity.wait_fixed(0))
    return AsyncTenacityBridgePolicy(
        domain="payment",
        rate_limit_coordinator=coordinator,
        rate_limit_key="payment",
        **kwargs,
    )


class TestAsyncBridgeCoordinationBehavior:
    """The async bridge waits through the awaitable twin and reports off the loop.

    The synchronous callbacks used to be reused verbatim, so the cooldown wait
    was a ``time.sleep`` inside ``AsyncRetrying`` — one request's cooldown
    froze every coroutine on the worker. The coroutine pair is awaited by
    tenacity natively; the execute-level reset and the final-outcome
    classification run on a worker thread when they install anything.
    """

    @pytest.mark.asyncio
    async def test_before_awaits_the_cooldown_on_every_attempt(self):
        coordinator = _async_coordinator()
        calls = {"n": 0}

        async def flaky():
            calls["n"] += 1
            if calls["n"] < 3:
                raise ConnectionError("transient")
            return "ok"

        result = await _keyed_async_bridge(
            coordinator, retry=tenacity.retry_if_exception_type(ConnectionError)
        ).execute(flaky)

        assert result.outcome == PolicyOutcome.SUCCESS
        assert coordinator.await_if_needed.await_count == 3
        assert coordinator.await_if_needed.call_args.args == ("payment",)
        coordinator.wait_if_needed.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_deferral_from_the_coroutine_before_aborts_the_loop(self):
        from baldur.services.rate_limit_coordinator.models import RateLimitResult

        coordinator = _async_coordinator()
        coordinator.await_if_needed.return_value = RateLimitResult(
            deferred=True, not_before=123.0
        )
        calls = {"n": 0}

        async def fn():
            calls["n"] += 1
            return "ok"

        result = await _keyed_async_bridge(coordinator).execute(fn)

        assert result.outcome == PolicyOutcome.FAILURE
        assert result.metadata["rate_limit_deferred"] is True
        assert result.metadata["not_before"] == 123.0
        assert calls["n"] == 0

    @pytest.mark.asyncio
    async def test_a_429_then_success_resets_the_ladder_once_on_a_worker_thread(self):
        from tests.factories.rate_limit_doubles import ToThreadSpy

        coordinator = _async_coordinator()
        calls = {"n": 0}

        async def flaky():
            calls["n"] += 1
            if calls["n"] == 1:
                raise _Throttled()
            return "ok"

        spy = ToThreadSpy()
        with patch(_POLICY_TO_THREAD, new=spy):
            result = await _keyed_async_bridge(coordinator).execute(flaky)

        assert result.outcome == PolicyOutcome.SUCCESS
        coordinator.on_success.assert_called_once_with("payment")
        assert coordinator.on_rate_limited.call_count == 1
        assert spy.count("_notify_success") == 1

    @pytest.mark.asyncio
    async def test_a_success_with_no_signal_resets_nothing_and_hops_nothing(self):
        """The success hot path pays no executor hop."""
        from tests.factories.rate_limit_doubles import ToThreadSpy

        coordinator = _async_coordinator()

        async def ok():
            return "ok"

        spy = ToThreadSpy()
        with patch(_POLICY_TO_THREAD, new=spy):
            result = await _keyed_async_bridge(coordinator).execute(ok)

        assert result.outcome == PolicyOutcome.SUCCESS
        coordinator.on_success.assert_not_called()
        assert spy.calls == []

    @pytest.mark.asyncio
    async def test_an_accepted_429_value_is_classified_on_a_worker_thread(self):
        """An unseen final 429 installs a cooldown off the loop and resets nothing."""
        from tests.factories.rate_limit_doubles import ToThreadSpy

        coordinator = _async_coordinator()

        async def throttled():
            return _throttled_response()

        spy = ToThreadSpy()
        with patch(_POLICY_TO_THREAD, new=spy):
            result = await _keyed_async_bridge(coordinator).execute(throttled)

        assert result.outcome == PolicyOutcome.SUCCESS
        coordinator.on_rate_limited.assert_called_once()
        coordinator.on_success.assert_not_called()
        assert spy.count("_classify_unseen_final_outcome") == 1

    @pytest.mark.asyncio
    async def test_a_declined_429_exception_is_classified_on_a_worker_thread(self):
        from tests.factories.rate_limit_doubles import ToThreadSpy

        coordinator = _async_coordinator()

        async def throttled():
            raise _Throttled()

        spy = ToThreadSpy()
        with patch(_POLICY_TO_THREAD, new=spy):
            result = await _keyed_async_bridge(
                coordinator, retry=tenacity.retry_if_exception_type(ValueError)
            ).execute(throttled)

        assert result.outcome == PolicyOutcome.FAILURE
        coordinator.on_rate_limited.assert_called_once()
        assert spy.count("_classify_unseen_final_outcome") == 1

    @pytest.mark.asyncio
    async def test_a_result_rejection_exhaustion_reports_failure(self):
        from baldur.services.retry_handler.models import MaxRetriesExceededError

        coordinator = _async_coordinator()

        async def throttled():
            return _throttled_response()

        result = await _keyed_async_bridge(
            coordinator, retry=tenacity.retry_if_result(lambda v: True)
        ).execute(throttled)

        assert result.outcome == PolicyOutcome.FAILURE
        assert isinstance(result.error, MaxRetriesExceededError)
        assert result.error.result_rejected is True
        assert coordinator.on_rate_limited.call_count == 3
        coordinator.on_success.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_user_fallback_after_a_result_rejection_exhaustion_is_a_failure(
        self,
    ):
        coordinator = _async_coordinator()

        async def throttled():
            return _throttled_response()

        result = await _keyed_async_bridge(
            coordinator,
            retry=tenacity.retry_if_result(lambda v: True),
            retry_error_callback=lambda _state: "user-default",
        ).execute(throttled)

        assert result.outcome == PolicyOutcome.FAILURE
        assert result.value == "user-default"
        assert result.metadata.get("user_callback_fallback") is True
        coordinator.on_success.assert_not_called()

    @pytest.mark.asyncio
    async def test_an_on_success_fault_is_fail_open(self):
        coordinator = _async_coordinator()
        coordinator.on_success.side_effect = RuntimeError("coordinator down")
        calls = {"n": 0}

        async def flaky():
            calls["n"] += 1
            if calls["n"] == 1:
                raise _Throttled()
            return "ok"

        result = await _keyed_async_bridge(coordinator).execute(flaky)

        assert result.outcome == PolicyOutcome.SUCCESS
        assert result.value == "ok"

    @pytest.mark.asyncio
    async def test_a_sync_user_before_still_runs_first_under_the_async_pair(self):
        order: list[str] = []
        coordinator = _async_coordinator()

        async def record_wait(*_args, **_kwargs):
            from baldur.services.rate_limit_coordinator.models import RateLimitResult

            order.append("baldur")
            return RateLimitResult(waited=False)

        coordinator.await_if_needed.side_effect = record_wait

        async def ok():
            return "ok"

        result = await _keyed_async_bridge(
            coordinator,
            stop=tenacity.stop_after_attempt(1),
            before=lambda _state: order.append("user"),
        ).execute(ok)

        assert result.outcome == PolicyOutcome.SUCCESS
        assert order == ["user", "baldur"]
