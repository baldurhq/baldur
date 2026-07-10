"""First-retry delay parity across the sync policy, async policy, and core primitive.

Regression coverage for the 0-indexed-attempt bug: the async retry policy and
the core retry primitive fed a 0-indexed loop attempt into the backoff
strategy's 1-indexed ``calculate()`` contract, so the first retry waited
``base_delay / multiplier`` instead of the configured ``base_delay`` — one
config produced different delay curves sync vs async vs core. All three paths
must now yield ``base_delay`` on the first retry.

Test targets:
    - core.backoff.BackoffStrategy.calculate: 1-indexed contract (attempt=1 ->
      base_delay) across all four strategies.
    - services.retry_handler.policy.RetryPolicy (sync), resilience.policies.
      async_retry.AsyncRetryPolicy (async), core.retry.retry_with_backoff
      (core): observed first-retry delay equals the configured base_delay.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from baldur.core.backoff import (
    ConstantBackoff,
    DecorrelatedJitterBackoff,
    ExponentialBackoff,
    LinearBackoff,
)
from baldur.core.retry import RetryConfig, retry_with_backoff
from baldur.resilience.policies.async_retry import AsyncRetryPolicy
from baldur.services.retry_handler.models import RetryPolicyConfig
from baldur.services.retry_handler.policy import RetryPolicy

# Distinctive, jitter-free delay: under the old 0-indexed bug the first retry
# would have waited _BASE_DELAY / multiplier (3.75) instead of _BASE_DELAY.
_BASE_DELAY = 7.5
_MULTIPLIER = 2.0


def _jitterless_backoff() -> ExponentialBackoff:
    return ExponentialBackoff(
        base_delay=_BASE_DELAY, multiplier=_MULTIPLIER, jitter=False
    )


class TestBackoffContract:
    """calculate() is 1-indexed: attempt=1 yields base_delay for every strategy."""

    @pytest.mark.parametrize(
        "strategy_factory",
        [
            lambda: ExponentialBackoff(
                base_delay=_BASE_DELAY, multiplier=_MULTIPLIER, jitter=False
            ),
            lambda: LinearBackoff(base_delay=_BASE_DELAY, jitter=False),
            lambda: ConstantBackoff(delay=_BASE_DELAY),
            lambda: DecorrelatedJitterBackoff(base_delay=_BASE_DELAY),
        ],
        ids=["exponential", "linear", "constant", "decorrelated"],
    )
    def test_calculate_attempt_one_returns_base_delay(self, strategy_factory):
        """First retry (attempt=1) yields the configured base delay, unscaled."""
        strategy = strategy_factory()

        assert strategy.calculate(1) == _BASE_DELAY

    def test_calculate_attempt_two_scales_exponential_by_one_multiplier_step(self):
        """Second retry (attempt=2) is base_delay * multiplier — one step, not two."""
        strategy = _jitterless_backoff()

        assert strategy.calculate(2) == _BASE_DELAY * _MULTIPLIER


class TestRetryFirstDelayParityBehavior:
    """Sync policy, async policy, and core primitive share one first-delay curve."""

    def _sync_first_delay(self) -> float:
        delays: list[float] = []
        policy = RetryPolicy(
            config=RetryPolicyConfig(max_attempts=2),
            backoff=_jitterless_backoff(),
            sleeper=delays.append,
        )

        def always_fail():
            raise ConnectionError("down")

        result = policy.execute(always_fail)

        assert result.success is False
        assert len(delays) == 1
        return delays[0]

    async def _async_first_delay(self) -> float:
        policy = AsyncRetryPolicy(max_retries=1, backoff=_jitterless_backoff())

        async def always_fail():
            raise ConnectionError("down")

        with patch(
            "baldur.resilience.policies.async_retry.asyncio.sleep",
            new_callable=AsyncMock,
        ) as mock_sleep:
            result = await policy.execute(always_fail)

        assert result.success is False
        assert mock_sleep.await_count == 1
        return mock_sleep.await_args_list[0].args[0]

    def _core_first_delay(self) -> float:
        def always_fail():
            raise ConnectionError("down")

        with patch("baldur.core.retry.time.sleep") as mock_sleep:
            outcome = retry_with_backoff(
                always_fail,
                RetryConfig(max_retries=2, backoff=_jitterless_backoff()),
            )

        assert outcome.success is False
        assert mock_sleep.call_count == 1
        return mock_sleep.call_args_list[0].args[0]

    def test_sync_policy_first_retry_waits_base_delay(self):
        """The sync policy's first retry waits exactly the configured base_delay."""
        assert self._sync_first_delay() == _BASE_DELAY

    @pytest.mark.asyncio
    async def test_async_policy_first_retry_waits_base_delay(self):
        """The async policy's first retry waits base_delay, not base_delay/multiplier."""
        assert await self._async_first_delay() == _BASE_DELAY

    def test_core_primitive_first_retry_waits_base_delay(self):
        """The core primitive's first retry waits base_delay, not base_delay/multiplier."""
        assert self._core_first_delay() == _BASE_DELAY

    def test_core_primitive_outcome_reports_first_delay_as_total_wait(self):
        """RetryOutcome.total_wait_seconds accumulates the corrected first delay."""

        def always_fail():
            raise ConnectionError("down")

        with patch("baldur.core.retry.time.sleep"):
            outcome = retry_with_backoff(
                always_fail,
                RetryConfig(max_retries=2, backoff=_jitterless_backoff()),
            )

        assert outcome.total_wait_seconds == _BASE_DELAY

    @pytest.mark.asyncio
    async def test_all_three_paths_yield_the_same_first_retry_delay(self):
        """One configured curve: sync, async, and core first delays are identical."""
        sync_delay = self._sync_first_delay()
        async_delay = await self._async_first_delay()
        core_delay = self._core_first_delay()

        assert sync_delay == async_delay == core_delay == _BASE_DELAY
