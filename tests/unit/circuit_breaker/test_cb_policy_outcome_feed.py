"""Unit tests for the policy-layer feed into the call-outcome window (746 D2/D10).

The failure-rate producer is fed from ``CircuitBreakerPolicy``, not from
``CircuitBreakerService.record_*``. That choice is the whole reason the rate can
be trusted, and it is what these tests pin:

- the service entry is driven by the precomputed-cache refresher every few
  seconds, by Django middleware that writes two successes per 2xx request and
  failures for rejected requests that never executed, and by the chaos inject
  endpoints — all of which would fabricate a rate on an idle worker;
- policy construction is user-entry only, so those writers are structurally
  excluded. The negative case below is what keeps that true.

Also covered: the key is resolved once at construction (never per call), the
paths that must record nothing (rejected, observe-only, disabled, ignored
exception), the fail-open contract on the recording side effect, and the
reset chain that keeps the module singleton from leaking across settings resets.

Verification techniques applied:
- State transition: success / counted failure / ignored exception
- Negative assertion: reject verdict, observe-only, CB disabled, internal
  recorder traffic
- Dependency interaction: the recorder is fed the resolved key, once per outcome
- Async parity: the same seam through ``AsyncCircuitBreakerPolicy``
- Fail-open: an exception inside recording never replaces the business result
- Reset chain completeness: ``reset_protect_caches()`` empties the window
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

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
from baldur.services.circuit_breaker.policy import (
    AsyncCircuitBreakerPolicy,
    CircuitBreakerPolicy,
)
from baldur.services.circuit_breaker.service import CircuitBreakerService
from baldur.services.circuit_breaker.time_outcome_window import (
    get_call_outcome_window,
    reset_call_outcome_window,
)

SERVICE = "checkout_api"


class _CountedError(Exception):
    """An exception the breaker is configured to count."""


class _IgnoredError(Exception):
    """An exception the breaker is configured to ignore."""


def _cb_service(**overrides) -> CircuitBreakerService:
    """A real breaker over the in-memory repository.

    The admission verdict is the proximate cause of every "records nothing"
    assertion below, so it is driven by the shipped state machine rather than by
    a stubbed decision.
    """
    base = {
        "enabled": True,
        "failure_threshold": 2,
        # The rate trigger is disabled so the trip point is exactly the
        # consecutive-failure count these tests drive.
        "failure_rate_threshold": 0.0,
        "sliding_window_size": 100,
    }
    base.update(overrides)
    return CircuitBreakerService(
        config=CircuitBreakerConfig(**base),
        repository=InMemoryCircuitBreakerStateRepository(),
    )


def _policy(service_name: str = SERVICE, **overrides) -> CircuitBreakerPolicy:
    """A policy over a fresh real breaker, counting only ``_CountedError``."""
    return CircuitBreakerPolicy(
        service_name=service_name,
        cb_service=_cb_service(**overrides),
        failure_exceptions=(_CountedError,),
        ignore_exceptions=(_IgnoredError,),
    )


def _observed(key: str = SERVICE) -> tuple[int, int]:
    """The window's entry for one key, or the no-evidence pair."""
    return get_call_outcome_window().snapshot().get(key, (0, 0))


@pytest.fixture(autouse=True)
def _isolated_window_singleton():
    """Drop the process-wide window around every case."""
    reset_call_outcome_window()
    yield
    reset_call_outcome_window()


# =============================================================================
# Behavior — the feed at the policy layer
# =============================================================================


class TestCircuitBreakerPolicyOutcomeFeedBehavior:
    """What a protected call contributes to the failure-rate window."""

    def test_key_is_resolved_once_at_construction(self):
        """The name is fixed for the policy's lifetime, so the projection is not
        paid per call.

        Folding it into the recording path would put a substitution pass and a
        module-table lookup on every protected call.
        """
        policy = _policy("Payment_API")

        assert policy._outcome_key == "payment_api"

    def test_successful_call_records_one_admission(self):
        """A success enlarges the denominator only."""
        policy = _policy()

        result = policy.execute(lambda: "ok")

        assert result.outcome == PolicyOutcome.SUCCESS
        assert _observed() == (0, 1)

    def test_counted_failure_records_a_failed_admission(self):
        """A failure the breaker counts is in both members of the ratio."""
        policy = _policy()

        with pytest.raises(_CountedError):
            policy.execute(lambda: (_ for _ in ()).throw(_CountedError("boom")))

        assert _observed() == (1, 1)

    def test_ignored_exception_records_nothing_at_all(self):
        """Behind the same gate as the breaker's own count.

        An admission whose exception the breaker was told to ignore is in
        neither the numerator nor the denominator — otherwise a service that
        raises only ignored exceptions would read as 0% failing rather than as
        unmeasured.
        """
        policy = _policy()

        with pytest.raises(_IgnoredError):
            policy.execute(lambda: (_ for _ in ()).throw(_IgnoredError("skip")))

        assert _observed() == (0, 0)

    def test_mixed_traffic_forms_the_rate(self):
        """Successes and counted failures accumulate into one ratio."""
        policy = _policy(failure_threshold=100)

        policy.execute(lambda: "ok")
        policy.execute(lambda: "ok")
        with pytest.raises(_CountedError):
            policy.execute(lambda: (_ for _ in ()).throw(_CountedError("boom")))

        assert _observed() == (1, 3)

    def test_rejected_call_records_nothing(self):
        """A fail-fast rejection is not an admission.

        Given/When/Then: the breaker is driven open by real failures, so the
        rejection verdict — not a stubbed decision — is the proximate cause of
        the totals staying put.
        """
        # Given: two counted failures trip the breaker
        policy = _policy(failure_threshold=2)
        for _ in range(2):
            with pytest.raises(_CountedError):
                policy.execute(lambda: (_ for _ in ()).throw(_CountedError("boom")))
        assert _observed() == (2, 2)

        # When: a further call is rejected without running
        result = policy.execute(lambda: "never runs")

        # Then: the window is unchanged
        assert result.outcome == PolicyOutcome.REJECTED
        assert _observed() == (2, 2)

    def test_disabled_breaker_records_nothing(self):
        """With the breaker off there are no admissions to count."""
        policy = _policy(enabled=False)

        result = policy.execute(lambda: "ok")

        assert result.outcome == PolicyOutcome.SUCCESS
        assert _observed() == (0, 0)

    def test_observe_only_mode_records_nothing(self):
        """Shadow mode promises to suppress every intervention and every count."""
        policy = _policy()
        set_execution_mode(ExecutionMode.shadow())
        try:
            result = policy.execute(lambda: "ok")
        finally:
            clear_execution_mode_override()

        assert result.outcome == PolicyOutcome.SUCCESS
        assert _observed() == (0, 0)

    def test_unprojectable_name_records_nothing_rather_than_a_wrong_row(self):
        """A policy whose key projection failed contributes no evidence.

        Negative assertion: recording under a placeholder would attribute this
        service's outcomes to a row naming something else.
        """
        with patch(
            "baldur.metrics.registry.canonicalize_domain_label",
            side_effect=RuntimeError("registry unavailable"),
        ):
            policy = _policy()
        assert policy._outcome_key is None

        policy.execute(lambda: "ok")

        assert get_call_outcome_window().snapshot() == {}

    def test_recorder_failure_does_not_fail_the_business_call(self):
        """Fail-open: recording is a side effect, and the fault is proven to fire."""

        class _RaisingWindowGetter:
            def __init__(self) -> None:
                self.touched = False

            def __call__(self):
                self.touched = True
                raise RuntimeError("window unavailable")

        policy = _policy()
        getter = _RaisingWindowGetter()

        with patch(
            "baldur.services.circuit_breaker.time_outcome_window"
            ".get_call_outcome_window",
            getter,
        ):
            result = policy.execute(lambda: "ok")

        assert getter.touched
        assert result.value == "ok"

    def test_recorder_failure_does_not_replace_the_business_exception(self):
        """The caller must still see its own error, not the recorder's."""

        class _RaisingWindowGetter:
            def __init__(self) -> None:
                self.touched = False

            def __call__(self):
                self.touched = True
                raise RuntimeError("window unavailable")

        policy = _policy()
        getter = _RaisingWindowGetter()

        with patch(
            "baldur.services.circuit_breaker.time_outcome_window"
            ".get_call_outcome_window",
            getter,
        ):
            with pytest.raises(_CountedError):
                policy.execute(lambda: (_ for _ in ()).throw(_CountedError("boom")))

        assert getter.touched

    def test_two_policies_on_names_that_merge_share_one_entry(self):
        """The collapse the design discloses, asserted rather than assumed.

        Two names differing only in label-unsafe characters project onto one key
        and therefore onto one row whose rate matches neither breaker. The
        producer warns the first time it happens; this pins the arithmetic.
        """
        first = _policy("Payment_API", failure_threshold=100)
        second = _policy("payment-api", failure_threshold=100)

        first.execute(lambda: "ok")
        with pytest.raises(_CountedError):
            second.execute(lambda: (_ for _ in ()).throw(_CountedError("boom")))

        assert get_call_outcome_window().snapshot() == {"payment_api": (1, 2)}


class TestAsyncCircuitBreakerPolicyOutcomeFeedBehavior:
    """The async wrapper drives the same seam, so one feed covers both forms."""

    def test_awaited_success_records_one_admission(self):
        """``aprotect()``-shaped traffic is measured identically."""
        policy = AsyncCircuitBreakerPolicy(_policy())

        async def _run():
            async def _func():
                return "ok"

            return await policy.execute(_func)

        result = asyncio.run(_run())

        assert result.outcome == PolicyOutcome.SUCCESS
        assert _observed() == (0, 1)

    def test_awaited_counted_failure_records_a_failed_admission(self):
        """The async failure branch reaches the same recorder."""
        policy = AsyncCircuitBreakerPolicy(_policy(failure_threshold=100))

        async def _run():
            async def _func():
                raise _CountedError("boom")

            await policy.execute(_func)

        with pytest.raises(_CountedError):
            asyncio.run(_run())

        assert _observed() == (1, 1)

    def test_cancelled_await_records_nothing(self):
        """A client disconnect is not a failed call.

        ``CancelledError`` is a ``BaseException``, so it escapes the policy's
        ``except Exception`` boundary untouched — the breaker does not count it,
        and neither does the rate window.
        """
        policy = AsyncCircuitBreakerPolicy(_policy(failure_threshold=100))

        async def _run():
            async def _func():
                raise asyncio.CancelledError()

            await policy.execute(_func)

        with pytest.raises(asyncio.CancelledError):
            asyncio.run(_run())

        assert _observed() == (0, 0)


# =============================================================================
# Negative — the internal recorders stay out of the window
# =============================================================================


class TestOutcomeWindowExcludesInternalTraffic:
    """Writers that reach ``CircuitBreakerService`` directly must not move the rate.

    This is the negative half of the D2 decision. The precomputed-cache
    refresher, ``BaldurMiddleware`` and the chaos inject endpoints all call the
    service recorders, and any of them would fabricate a non-null rate on an
    idle worker.
    """

    def test_service_level_success_moves_nothing(self):
        """``record_success`` is the internal-daemon path, not an admission."""
        service = _cb_service()

        service.record_success(SERVICE)

        assert get_call_outcome_window().read_all() == (0, 0)

    def test_service_level_failure_moves_nothing(self):
        """``record_failure`` likewise — including the chaos inject endpoints."""
        service = _cb_service(failure_threshold=100)

        service.record_failure(SERVICE, error_context={"error": "synthetic"})

        assert get_call_outcome_window().read_all() == (0, 0)

    def test_repeated_internal_writes_leave_the_window_empty(self):
        """An idle worker whose refresher ticks stays honestly unmeasured."""
        service = _cb_service(failure_threshold=100)

        for _ in range(5):
            service.record_success(SERVICE)

        assert get_call_outcome_window().snapshot() == {}


# =============================================================================
# Behavior — the reset chain (D10)
# =============================================================================


class TestProtectResetChainClearsOutcomeWindow:
    """``reset_protect_caches()`` has to drop the window explicitly.

    Unlike the per-breaker ``OutcomeWindow``, which the policy-cache clear
    discards implicitly, this producer is a module singleton — so without an
    explicit drop its evidence leaks across settings-reset boundaries and one
    test's traffic becomes another's rows.
    """

    def test_reset_protect_caches_empties_the_window(self):
        """Recorded evidence does not survive a protect-cache reset."""
        from baldur.protect_facade import reset_protect_caches

        get_call_outcome_window().record(SERVICE, failure=True)
        assert get_call_outcome_window().snapshot() != {}

        reset_protect_caches()

        assert get_call_outcome_window().snapshot() == {}

    def test_reset_protect_caches_replaces_the_window_instance(self):
        """The drop is a singleton replacement, so no stale reference survives."""
        from baldur.protect_facade import reset_protect_caches

        first = get_call_outcome_window()

        reset_protect_caches()

        assert get_call_outcome_window() is not first
