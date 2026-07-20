"""Unit tests for ``baldur.bridges.tenacity.policy.TenacityBridgePolicy`` (impl 451).

Scope:
- ``execute()`` — success / exhausted / budget-exhausted / rate-limit / vanilla / user-fallback paths.
- ``execute()`` — ``total_attempts`` matches actual fn invocation count (D5 attempt counter).
- ``from_existing()`` — strategy + callback extraction from an existing Retrying instance.
- ``__init__()`` — ImportError when tenacity extra missing.
- ``__baldur_bridge_explicit__`` marker on every internally-built Retrying.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
import tenacity

from baldur.adapters.rate_limit.memory_adapter import InMemoryRateLimitStorage
from baldur.bridges.tenacity.policy import (
    _BRIDGE_EXPLICIT_MARKER,
    AsyncTenacityBridgePolicy,
    TenacityBridgePolicy,
)
from baldur.interfaces.resilience_policy import PolicyOutcome
from baldur.services.backoff_calculator.budget import AdaptiveRetryBudget
from baldur.services.rate_limit_coordinator import RateLimitCoordinator
from baldur.services.rate_limit_coordinator.models import RateLimitResult

# =============================================================================
# Helpers
# =============================================================================


def _make_counting_fn(failures_before_success: int):
    """Return ``(fn, counter_dict)`` where ``fn`` raises N times then returns 'ok'."""
    counter = {"calls": 0}

    def _fn():
        counter["calls"] += 1
        if counter["calls"] <= failures_before_success:
            raise ValueError(f"transient {counter['calls']}")
        return "ok"

    return _fn, counter


def _make_always_failing_fn():
    counter = {"calls": 0}

    def _fn():
        counter["calls"] += 1
        raise RuntimeError("permanent")

    return _fn, counter


# =============================================================================
# Behavior — execute() success after N attempts
# =============================================================================


class TestTenacityBridgePolicyExecuteSuccessBehavior:
    """``execute()`` returns SUCCESS PolicyResult after N tenacity attempts."""

    @pytest.mark.parametrize(
        ("failures_before_success", "max_attempts"),
        [
            (0, 3),  # immediate success
            (1, 3),  # 1 failure then success
            (2, 3),  # exhaust 2 then success on 3rd
            (4, 5),  # 4 failures then 5th succeeds
        ],
        ids=["immediate", "one_retry", "two_retries", "four_retries"],
    )
    def test_success_returns_value_and_attempt_count_matches_invocations(
        self, failures_before_success, max_attempts
    ):
        """SUCCESS result carries the actual invocation count from RetryCallState."""
        # Given
        fn, counter = _make_counting_fn(failures_before_success)
        policy: TenacityBridgePolicy[str] = TenacityBridgePolicy(
            stop=tenacity.stop_after_attempt(max_attempts),
            wait=tenacity.wait_fixed(0),
        )

        # When
        result = policy.execute(fn)

        # Then
        assert result.outcome == PolicyOutcome.SUCCESS
        assert result.value == "ok"
        assert result.total_attempts == counter["calls"]
        assert result.total_attempts == failures_before_success + 1


# =============================================================================
# Behavior — execute() exhausted (no user retry_error_callback)
# =============================================================================


class TestTenacityBridgePolicyExhaustedBehavior:
    """When all attempts fail and no user fallback, return FAILURE with the error."""

    def test_all_failures_return_failure_with_last_error(self):
        """FAILURE outcome carries the underlying exception."""
        fn, counter = _make_always_failing_fn()
        policy: TenacityBridgePolicy[str] = TenacityBridgePolicy(
            stop=tenacity.stop_after_attempt(3),
            wait=tenacity.wait_fixed(0),
        )

        result = policy.execute(fn)

        assert result.outcome == PolicyOutcome.FAILURE
        assert isinstance(result.error, RuntimeError)
        assert result.total_attempts == 3
        assert counter["calls"] == 3

    def test_non_retryable_after_retries_reports_actual_attempt_count(self):
        """Non-retryable exception on attempt N → ``total_attempts`` reflects N.

        Regression for the ``getattr(retrying.statistics, "attempt_number", 1)``
        bug: ``Retrying.statistics`` is a dict, so attribute-style access always
        returned the default (1) instead of the real attempt counter. Behavior
        is verified by computing the expected value from the fn invocation
        counter rather than hardcoding it.
        """

        class _Retryable(Exception):
            pass

        class _NonRetryable(Exception):
            pass

        counter = {"calls": 0}

        def _fn():
            counter["calls"] += 1
            if counter["calls"] < 3:
                raise _Retryable("transient")
            raise _NonRetryable("permanent")

        policy: TenacityBridgePolicy[str] = TenacityBridgePolicy(
            stop=tenacity.stop_after_attempt(5),
            wait=tenacity.wait_fixed(0),
            retry=tenacity.retry_if_exception_type(_Retryable),
        )

        result = policy.execute(_fn)

        assert result.outcome == PolicyOutcome.FAILURE
        assert isinstance(result.error, _NonRetryable)
        assert result.total_attempts == counter["calls"]
        assert counter["calls"] == 3

    def test_exhausted_emits_retry_exhausted_event(self, monkeypatch):
        """RETRY_EXHAUSTED is emitted on the EventBus with source='tenacity_bridge'."""
        from baldur.bridges.tenacity import callbacks as callbacks_mod

        emitted: list[dict] = []

        class _StubBus:
            def emit(self, *, event_type, data, source):
                emitted.append(
                    {"event_type": event_type, "data": data, "source": source}
                )

        monkeypatch.setattr(
            "baldur.services.event_bus.get_event_bus", lambda: _StubBus()
        )

        fn, _ = _make_always_failing_fn()
        policy: TenacityBridgePolicy[str] = TenacityBridgePolicy(
            stop=tenacity.stop_after_attempt(2),
            wait=tenacity.wait_fixed(0),
            domain="payment_api",
        )
        policy.execute(fn)

        from baldur.services.event_bus.bus.event_types import EventType

        assert len(emitted) == 1
        assert emitted[0]["event_type"] == EventType.RETRY_EXHAUSTED
        assert emitted[0]["source"] == "tenacity_bridge"
        assert emitted[0]["data"]["domain"] == "payment_api"
        assert emitted[0]["data"]["attempts"] == 2
        assert emitted[0]["data"]["final_error_type"] == "RuntimeError"
        # Silence accidental reuse from import elsewhere
        del callbacks_mod


# =============================================================================
# Behavior — budget-exhausted abort
# =============================================================================


class TestTenacityBridgePolicyBudgetGuardBehavior:
    """``AdaptiveRetryBudget.should_allow_retry()=False`` aborts the retry loop."""

    def test_budget_exhausted_short_circuits_loop(self):
        """When budget rejects, no further attempts run after the first failure."""
        fn, counter = _make_always_failing_fn()
        budget = AdaptiveRetryBudget(max_retry_ratio=0.0)  # always reject

        policy: TenacityBridgePolicy[str] = TenacityBridgePolicy(
            stop=tenacity.stop_after_attempt(5),
            wait=tenacity.wait_fixed(0),
            retry_budget=budget,
        )
        result = policy.execute(fn)

        # 1st attempt runs, before_sleep raises _BudgetExhaustedAbort.
        assert counter["calls"] == 1
        assert result.outcome == PolicyOutcome.FAILURE
        assert result.metadata.get("budget_exhausted") is True


# =============================================================================
# Behavior — rate-limit injection
# =============================================================================


class TestTenacityBridgePolicyRateLimitBehavior:
    """RateLimitCoordinator hooks fire on each attempt when key is provided."""

    def test_wait_if_needed_called_per_attempt(self):
        """``before(retry_state)`` calls coordinator.wait_if_needed(key) on every attempt."""
        # tenacity fires ``before`` on every attempt, so two attempts → two calls.
        fn, counter = _make_counting_fn(failures_before_success=1)
        coord = MagicMock()
        # Real result object, not a MagicMock: an auto-generated ``.deferred``
        # attribute is truthy and would abort the loop before any attempt runs.
        coord.wait_if_needed.return_value = RateLimitResult(waited=False, wait_time=0.0)

        policy: TenacityBridgePolicy[str] = TenacityBridgePolicy(
            stop=tenacity.stop_after_attempt(3),
            wait=tenacity.wait_fixed(0),
            rate_limit_coordinator=coord,
            rate_limit_key="payment_api",
        )
        policy.execute(fn)

        # tenacity ``after`` fires once (after the failed first attempt) and
        # the bridge calls on_success only when the outcome is non-failed.
        # ``before`` fires twice (attempts 1 and 2), so wait_if_needed runs twice.
        assert coord.wait_if_needed.call_count == counter["calls"]
        assert coord.wait_if_needed.call_args_list[0].args == ("payment_api",)

    def test_failure_with_429_invokes_on_rate_limited(self):
        """``after(retry_state)`` invokes on_rate_limited when error is 429-like."""

        # 429-detectable exception: HTTPError with status_code attr per
        # baldur.services.retry_handler.rate_limit_detection.detect_rate_limit.
        class _RateLimited(Exception):
            def __init__(self):
                super().__init__("rate limit")
                self.status_code = 429
                self.response = MagicMock(headers={"Retry-After": "5"})

        coord = MagicMock()
        # Real result object, not a MagicMock: an auto-generated ``.deferred``
        # attribute is truthy and would abort the loop before any attempt runs.
        coord.wait_if_needed.return_value = RateLimitResult(waited=False, wait_time=0.0)

        def _fail_429():
            raise _RateLimited()

        policy: TenacityBridgePolicy[str] = TenacityBridgePolicy(
            stop=tenacity.stop_after_attempt(1),
            rate_limit_coordinator=coord,
            rate_limit_key="payment_api",
        )
        policy.execute(_fail_429)

        # on_rate_limited called at least once with the same key.
        assert coord.on_rate_limited.called
        assert coord.on_rate_limited.call_args.kwargs["key"] == "payment_api"


# =============================================================================
# Behavior — user retry_error_callback fallback
# =============================================================================


class TestTenacityBridgePolicyUserCallbackFallbackBehavior:
    """User ``retry_error_callback`` returning fallback is preserved with metadata."""

    def test_user_fallback_value_propagates_with_failure_outcome(self):
        """Bridge returns FAILURE with value=user_fallback and metadata flag."""
        fn, _ = _make_always_failing_fn()

        def _fallback(_retry_state):
            return "user-default"

        policy: TenacityBridgePolicy[str] = TenacityBridgePolicy(
            stop=tenacity.stop_after_attempt(2),
            wait=tenacity.wait_fixed(0),
            retry_error_callback=_fallback,
        )
        result = policy.execute(fn)

        assert result.outcome == PolicyOutcome.FAILURE
        assert result.value == "user-default"
        assert result.metadata.get("user_callback_fallback") is True
        assert isinstance(result.error, RuntimeError)


# =============================================================================
# Behavior — vanilla path (no collaborators)
# =============================================================================


class TestTenacityBridgePolicyVanillaBehavior:
    """When all collaborators are None, bridge behaves as vanilla tenacity."""

    def test_vanilla_success_runs_with_no_side_effects(self):
        """No coordinator/budget — fn invocation counter still matches attempts."""
        fn, counter = _make_counting_fn(1)
        policy: TenacityBridgePolicy[str] = TenacityBridgePolicy(
            stop=tenacity.stop_after_attempt(3),
            wait=tenacity.wait_fixed(0),
        )

        result = policy.execute(fn)

        assert result.outcome == PolicyOutcome.SUCCESS
        assert result.value == "ok"
        assert counter["calls"] == 2
        assert result.total_attempts == 2


# =============================================================================
# Contract — from_existing() strategy extraction
# =============================================================================


class TestTenacityBridgePolicyFromExistingContract:
    """``from_existing()`` extracts public attrs and reuses them per execute()."""

    @pytest.mark.parametrize(
        ("stop", "wait", "retry_pred"),
        [
            (
                tenacity.stop_after_attempt(2),
                tenacity.wait_fixed(0),
                tenacity.retry_if_exception_type(IOError),
            ),
            (
                tenacity.stop_after_delay(1),
                tenacity.wait_exponential(multiplier=0),
                tenacity.retry_if_exception_type(ValueError),
            ),
            (
                tenacity.stop_after_attempt(1),
                tenacity.wait_fixed(0),
                tenacity.retry_if_exception_type(RuntimeError),
            ),
        ],
        ids=[
            "after_attempt_fixed_io",
            "after_delay_exp_value",
            "single_attempt_runtime",
        ],
    )
    def test_extracts_stop_wait_retry_from_retrying_instance(
        self, stop, wait, retry_pred
    ):
        """The policy's stored attributes match the source Retrying instance."""
        original = tenacity.Retrying(stop=stop, wait=wait, retry=retry_pred)

        policy: TenacityBridgePolicy[None] = TenacityBridgePolicy.from_existing(
            original
        )

        assert policy._stop is stop
        assert policy._wait is wait
        assert policy._retry is retry_pred

    def test_extracts_user_callbacks_from_retrying_instance(self):
        """User-defined callbacks are picked up and re-applied on each execute()."""

        def _user_before(_state):
            pass

        def _user_after(_state):
            pass

        original = tenacity.Retrying(
            stop=tenacity.stop_after_attempt(1),
            before=_user_before,
            after=_user_after,
        )
        policy: TenacityBridgePolicy[None] = TenacityBridgePolicy.from_existing(
            original
        )
        assert policy._user_before is _user_before
        assert policy._user_after is _user_after


# =============================================================================
# Contract — ImportError when extra missing
# =============================================================================


class TestTenacityBridgePolicyImportGuardContract:
    """When ``_TENACITY_AVAILABLE`` is False, ``__init__`` raises ImportError."""

    def test_init_raises_when_tenacity_unavailable(self, monkeypatch):
        """ImportError mentions the install command from impl 451 D2."""
        monkeypatch.setattr("baldur.bridges.tenacity._TENACITY_AVAILABLE", False)

        with pytest.raises(ImportError, match=r"baldur-framework\[tenacity\]"):
            TenacityBridgePolicy(stop=tenacity.stop_after_attempt(1))


# =============================================================================
# Contract — explicit marker on every Retrying built by the policy
# =============================================================================


class TestTenacityBridgePolicyExplicitMarkerContract:
    """Every Retrying built inside execute() carries the explicit marker (D5↔D7)."""

    def test_explicit_marker_set_on_internal_retrying(self):
        """Marker visible after execute() — needed for instrument-skip behavior."""
        captured: list[tenacity.Retrying] = []

        original_init = tenacity.Retrying.__init__

        def _capture_init(self, *args, **kwargs):
            original_init(self, *args, **kwargs)
            captured.append(self)

        # Patch only for this test; conftest reset still owns global cleanup.
        try:
            tenacity.Retrying.__init__ = _capture_init  # type: ignore[method-assign]
            fn, _ = _make_counting_fn(0)
            policy: TenacityBridgePolicy[str] = TenacityBridgePolicy(
                stop=tenacity.stop_after_attempt(1)
            )
            policy.execute(fn)
        finally:
            tenacity.Retrying.__init__ = original_init  # type: ignore[method-assign]

        assert captured, "Retrying must be constructed by execute()"
        assert getattr(captured[-1], _BRIDGE_EXPLICIT_MARKER, False) is True

    def test_marker_attribute_name_is_baldur_bridge_explicit(self):
        """Contract: marker name matches D5/D7 spec."""
        assert _BRIDGE_EXPLICIT_MARKER == "__baldur_bridge_explicit__"


# =============================================================================
# Behavior — bounded cooldown wait: deferral + bound forwarding (D6)
# =============================================================================


_LONG_COOLDOWN_SECONDS = 300.0


def _coordinator_in_cooldown(key: str):
    """Real coordinator with a shared cooldown far longer than any test bound."""
    import time

    storage = InMemoryRateLimitStorage()
    storage.set_cooldown(key, time.time() + _LONG_COOLDOWN_SECONDS)
    return RateLimitCoordinator(storage=storage)


class TestTenacityBridgeCooldownDeferralBehavior:
    """An over-bound cooldown aborts the bridge loop into a FAILURE with defer metadata."""

    @pytest.fixture(autouse=True)
    def _coordinator_singleton_hygiene(self):
        """Keep the bound-forwarding tests from leaking the coordinator singleton.

        A bridge built with ``rate_limit_key`` but no explicit coordinator
        resolves ``RateLimitCoordinator.get_instance()``, so the three forwarding
        tests below would leave a process-wide instance (and its cooldown timers)
        behind for every later test in the same xdist worker
        (UNIT_TEST_GUIDELINES 6.5).
        """
        RateLimitCoordinator.reset_instance()
        yield
        RateLimitCoordinator.reset_instance()

    def test_sync_deferral_returns_failure_with_defer_metadata(self):
        """Sync execute: deferral -> FAILURE, rate_limit_deferred + not_before, func skipped."""
        coord = _coordinator_in_cooldown("api")
        fn, counter = _make_counting_fn(0)
        policy: TenacityBridgePolicy[str] = TenacityBridgePolicy(
            stop=tenacity.stop_after_attempt(3),
            domain="api",
            rate_limit_coordinator=coord,
            rate_limit_key="api",
            rate_limit_max_wait=1.0,
        )

        result = policy.execute(fn)

        assert result.outcome == PolicyOutcome.FAILURE
        assert result.metadata["rate_limit_deferred"] is True
        assert result.metadata["not_before"] is not None
        assert counter["calls"] == 0  # the attempt was never made

    def test_async_deferral_returns_failure_with_defer_metadata(self):
        """Async execute mirrors the sync deferral translation."""
        import asyncio

        coord = _coordinator_in_cooldown("api")

        async def afn():
            return "ok"

        policy: AsyncTenacityBridgePolicy[str] = AsyncTenacityBridgePolicy(
            stop=tenacity.stop_after_attempt(3),
            domain="api",
            rate_limit_coordinator=coord,
            rate_limit_key="api",
            rate_limit_max_wait=1.0,
        )

        result = asyncio.run(policy.execute(afn))

        assert result.outcome == PolicyOutcome.FAILURE
        assert result.metadata["rate_limit_deferred"] is True
        assert result.metadata["not_before"] is not None

    def test_constructor_stores_the_bound(self):
        policy: TenacityBridgePolicy[str] = TenacityBridgePolicy(
            rate_limit_key="api", rate_limit_max_wait=2.5
        )
        assert policy._rate_limit_max_wait == 2.5

    def test_from_existing_forwards_the_bound(self):
        policy = TenacityBridgePolicy.from_existing(
            tenacity.Retrying(),
            domain="api",
            rate_limit_key="api",
            rate_limit_max_wait=2.5,
        )
        assert policy._rate_limit_max_wait == 2.5

    def test_from_sync_preserves_the_bound(self):
        """The sync->async field-by-field copy must not drop the bound (silent-drop site)."""
        sync_bridge: TenacityBridgePolicy[str] = TenacityBridgePolicy(
            rate_limit_key="api", rate_limit_max_wait=1.0
        )

        async_bridge = AsyncTenacityBridgePolicy.from_sync(sync_bridge)

        assert async_bridge._rate_limit_max_wait == 1.0
        # Negative: it is the forwarded value, not the constructor default.
        assert async_bridge._rate_limit_max_wait is not None


class TestTenacityBridgeCoordinatorFailOpenBehavior:
    """A coordinator fault in a bridge callback never changes the business outcome.

    The ``after`` hook only runs when a retry follows, so its fault sites are
    driven with a failing first attempt (on_rate_limited) and a retry_if_result
    rejection (on_success) — a plain success never reaches them.
    """

    def _coordinator_mock(self):
        coord = MagicMock(spec=RateLimitCoordinator)
        coord.wait_if_needed.return_value = RateLimitResult(waited=False)
        return coord

    def test_before_wait_fault_is_fail_open(self):
        """Site 4: wait_if_needed raises in ``before`` -> the loop still runs."""
        coord = self._coordinator_mock()
        coord.wait_if_needed.side_effect = RuntimeError("coordinator down")
        fn, counter = _make_counting_fn(0)
        policy: TenacityBridgePolicy[str] = TenacityBridgePolicy(
            stop=tenacity.stop_after_attempt(1),
            rate_limit_coordinator=coord,
            rate_limit_key="api",
        )

        result = policy.execute(fn)

        assert result.outcome == PolicyOutcome.SUCCESS
        assert result.value == "ok"

    def test_after_on_rate_limited_fault_does_not_truncate_loop(self):
        """Site 6: a fault recording the first 429 must not skip attempt 2."""

        class _RateLimited(Exception):
            """429-detectable via its message (rate_limit_detection)."""

        coord = self._coordinator_mock()
        coord.on_rate_limited.side_effect = RuntimeError("coordinator down")
        attempts = {"n": 0}

        def flaky():
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise _RateLimited("429 too many requests")
            return "recovered"

        policy: TenacityBridgePolicy[str] = TenacityBridgePolicy(
            stop=tenacity.stop_after_attempt(3),
            retry=tenacity.retry_if_exception_type(_RateLimited),
            wait=tenacity.wait_fixed(0),
            rate_limit_coordinator=coord,
            rate_limit_key="api",
        )

        result = policy.execute(flaky)

        assert result.outcome == PolicyOutcome.SUCCESS
        assert result.value == "recovered"
        assert attempts["n"] == 2  # attempt 2 ran — loop not truncated

    def test_after_429_detection_fault_does_not_truncate_loop(self):
        """Site 6: the fault surface includes 429 *detection*, not just the notify call.

        ``detect_rate_limit`` reads attributes off the caller's exception, so a
        response object without the expected shape raises inside the ``after``
        hook. Left outside the wrap it escapes tenacity's un-guarded ``after``,
        truncating the loop at attempt 1 and replacing the business error — the
        exact blast radius the wrap exists to prevent. The retry loop's
        equivalent wrap has always covered detection.
        """

        class _BadHeaders:
            headers = ["Retry-After: 5"]  # a list has no .get()

        business_error = RuntimeError("429 too many requests")
        business_error.response = _BadHeaders()

        coord = self._coordinator_mock()
        attempts = {"n": 0}

        def always_429():
            attempts["n"] += 1
            raise business_error

        policy: TenacityBridgePolicy[str] = TenacityBridgePolicy(
            stop=tenacity.stop_after_attempt(3),
            retry=tenacity.retry_if_exception_type(RuntimeError),
            wait=tenacity.wait_fixed(0),
            rate_limit_coordinator=coord,
            rate_limit_key="api",
        )

        result = policy.execute(always_429)

        assert attempts["n"] == 3  # loop not truncated
        # Negative: the detection fault never becomes the reported error.
        assert not isinstance(result.error, AttributeError)
        assert result.error is business_error

    def test_after_on_success_fault_is_fail_open(self):
        """Site 5: on_success raises on the retry-driving ``after`` -> result preserved."""
        coord = self._coordinator_mock()
        coord.on_success.side_effect = RuntimeError("coordinator down")
        results = {"n": 0}

        def rejecting():
            results["n"] += 1
            return "BAD" if results["n"] == 1 else "good"

        policy: TenacityBridgePolicy[str] = TenacityBridgePolicy(
            stop=tenacity.stop_after_attempt(3),
            retry=tenacity.retry_if_result(lambda v: v == "BAD"),
            wait=tenacity.wait_fixed(0),
            rate_limit_coordinator=coord,
            rate_limit_key="api",
        )

        result = policy.execute(rejecting)

        assert result.outcome == PolicyOutcome.SUCCESS
        assert result.value == "good"
