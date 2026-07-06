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
