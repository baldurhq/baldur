"""Async parity + ``@retry`` decorator tests for result-predicate + budget (704).

Target: ``resilience/policies/async_retry.py`` — ``AsyncRetryPolicy`` mirrors the
sync ``RetryPolicy`` result-predicate (D1) and cooperative wall-clock budget
(D2), recording ``metadata["reason"]`` (its event/metric emission is a parked
parity gap, so only the data-level reason is asserted here). The unified
``@retry`` decorator dual-dispatches on sync vs async and threads the new
``retry_on_result`` / ``max_elapsed`` params through both branches; its
double-wrap guard re-raises a synthesized ``MaxRetriesExceededError`` as-is
rather than nesting it.

All backoff is ``ConstantBackoff(0.0)`` so ``asyncio.sleep(0)`` / the skipped
sync sleep add no real wall-clock time; where elapsed must advance, a
deterministic ``_AdvancingClock`` replaces the module's ``time.monotonic``.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from baldur.core.backoff import ConstantBackoff
from baldur.interfaces.resilience_policy import PolicyOutcome
from baldur.resilience.policies.async_retry import AsyncRetryPolicy, retry
from baldur.services.retry_handler.models import MaxRetriesExceededError

_GET_REMAINING_MS = "baldur.scaling.deadline_context.get_remaining_ms"
_ASYNC_MONOTONIC = "baldur.resilience.policies.async_retry.time.monotonic"
_SYNC_MONOTONIC = "baldur.services.retry_handler.policy.time.monotonic"


class _AdvancingClock:
    """A deterministic monotonic clock advanced only by the business function."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _raising_predicate(result: object) -> bool:
    """A broken predicate that always raises (drives the fail-open path)."""
    raise RuntimeError("predicate broken")


# =============================================================================
# Behavior — AsyncRetryPolicy predicate + budget parity with the sync policy
# =============================================================================


class TestAsyncRetryPredicateBudgetBehavior:
    """``AsyncRetryPolicy`` mirrors the sync result-predicate and budget."""

    @pytest.mark.asyncio
    async def test_matching_result_retried_until_a_good_result_succeeds(self):
        values = iter(["bad", "good"])

        async def fn():
            return next(values)

        policy = AsyncRetryPolicy(
            max_retries=3,
            backoff=ConstantBackoff(delay=0.0),
            retry_on_result=lambda r: r == "bad",
            domain="test_async_predicate",
        )
        result = await policy.execute(fn)

        assert result.outcome == PolicyOutcome.SUCCESS
        assert result.value == "good"
        assert result.total_attempts == 2

    @pytest.mark.asyncio
    async def test_result_exhaustion_synthesizes_max_retries_error(self):
        payload = {"status": "error"}

        async def fn():
            return payload

        policy = AsyncRetryPolicy(
            max_retries=1,
            backoff=ConstantBackoff(delay=0.0),
            retry_on_result=lambda r: True,
            domain="test_async_predicate",
        )
        result = await policy.execute(fn)

        assert result.outcome == PolicyOutcome.FAILURE
        assert isinstance(result.error, MaxRetriesExceededError)
        assert result.error.is_result_exhaustion is True
        assert result.error.last_result == payload
        assert result.value == payload
        assert result.metadata["reason"] == "max_attempts"

    @pytest.mark.asyncio
    async def test_predicate_exception_fails_open_accepts_result(self):
        calls: list[int] = []

        async def fn():
            calls.append(1)
            return "v"

        policy = AsyncRetryPolicy(
            max_retries=3,
            backoff=ConstantBackoff(delay=0.0),
            retry_on_result=_raising_predicate,
            domain="test_async_predicate",
        )
        result = await policy.execute(fn)

        assert result.outcome == PolicyOutcome.SUCCESS
        assert result.value == "v"
        assert result.total_attempts == 1
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_budget_exit_records_metadata_reason_max_elapsed(self):
        clock = _AdvancingClock()

        async def failing():
            clock.advance(0.2)  # blows the 0.1s budget in one attempt
            raise ConnectionError("fail")

        policy = AsyncRetryPolicy(
            max_retries=5,
            backoff=ConstantBackoff(delay=0.0),
            max_elapsed=0.1,
            domain="test_async_budget",
        )
        with patch(_ASYNC_MONOTONIC, clock):
            result = await policy.execute(failing)

        assert result.outcome == PolicyOutcome.FAILURE
        assert result.metadata["reason"] == "max_elapsed"
        assert result.total_attempts == 1

    @pytest.mark.asyncio
    async def test_attempt_one_always_runs_under_expired_budget(self):
        async def succeed():
            return "ok"

        policy = AsyncRetryPolicy(
            max_retries=3,
            backoff=ConstantBackoff(delay=0.0),
            domain="test_async_budget",
        )
        with patch(_GET_REMAINING_MS, return_value=0.0):
            result = await policy.execute(succeed)

        assert result.outcome == PolicyOutcome.SUCCESS
        assert result.total_attempts == 1


# =============================================================================
# Behavior — the @retry decorator threads the new params through both branches
# =============================================================================


class TestRetryDecoratorBehavior:
    """``@retry`` dual-dispatches predicate + budget; the double-wrap guard holds."""

    def test_sync_function_retries_on_soft_result(self):
        values = iter(["bad", "good"])

        @retry(
            domain="test_dec",
            max_attempts=3,
            retry_on_result=lambda r: r == "bad",
            backoff=ConstantBackoff(delay=0.0),
        )
        def fn():
            return next(values)

        assert fn() == "good"

    @pytest.mark.asyncio
    async def test_async_function_retries_on_soft_result(self):
        values = iter(["bad", "good"])

        @retry(
            domain="test_dec",
            max_attempts=3,
            retry_on_result=lambda r: r == "bad",
            backoff=ConstantBackoff(delay=0.0),
        )
        async def fn():
            return next(values)

        assert await fn() == "good"

    def test_sync_predicate_exception_fails_open(self):
        @retry(
            domain="test_dec",
            max_attempts=3,
            retry_on_result=_raising_predicate,
            backoff=ConstantBackoff(delay=0.0),
        )
        def fn():
            return "v"

        assert fn() == "v"

    @pytest.mark.asyncio
    async def test_async_predicate_exception_fails_open(self):
        @retry(
            domain="test_dec",
            max_attempts=3,
            retry_on_result=_raising_predicate,
            backoff=ConstantBackoff(delay=0.0),
        )
        async def fn():
            return "v"

        assert await fn() == "v"

    def test_result_exhaustion_raises_a_single_max_retries_error(self):
        """The synthesized result-exhaustion error is re-raised as-is — NOT
        nested inside a second MaxRetriesExceededError (double-wrap guard)."""
        payload = {"status": "error"}

        @retry(
            domain="test_dec_double",
            max_attempts=2,
            retry_on_result=lambda r: True,
            backoff=ConstantBackoff(delay=0.0),
        )
        def soft_error():
            return payload

        with pytest.raises(MaxRetriesExceededError) as exc_info:
            soft_error()

        assert exc_info.value.is_result_exhaustion is True
        assert not isinstance(exc_info.value.last_error, MaxRetriesExceededError)
        assert exc_info.value.last_result == payload

    def test_max_elapsed_threads_through_sync_decorator_and_fires(self):
        """The decorator's ``max_elapsed`` reaches the policy and stops the loop."""
        clock = _AdvancingClock()

        @retry(
            domain="test_dec_budget",
            max_attempts=5,
            retryable_exceptions=(ConnectionError,),
            max_elapsed=0.1,
            backoff=ConstantBackoff(delay=0.0),
        )
        def always_fail():
            clock.advance(0.2)  # blows the 0.1s budget in one attempt
            raise ConnectionError("x")

        with patch(_SYNC_MONOTONIC, clock):
            with pytest.raises(MaxRetriesExceededError) as exc_info:
                always_fail()

        assert exc_info.value.retry_count == 1
        assert isinstance(exc_info.value.last_error, ConnectionError)
