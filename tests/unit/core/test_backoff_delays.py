"""Behavior tests for BackoffStrategy.delays(n) (the schedule helper RQ composes)."""

from __future__ import annotations

from baldur.core.backoff import (
    ConstantBackoff,
    DecorrelatedJitterBackoff,
    ExponentialBackoff,
)


class TestBackoffDelays:
    def test_exponential_delays_are_one_indexed(self):
        strat = ExponentialBackoff(
            base_delay=1.0, multiplier=2.0, max_delay=60.0, jitter=False
        )
        assert strat.delays(4) == [1.0, 2.0, 4.0, 8.0]

    def test_delays_matches_calculate_sequence(self):
        strat = ExponentialBackoff(
            base_delay=2.0, multiplier=3.0, max_delay=100.0, jitter=False
        )
        assert strat.delays(3) == [
            strat.calculate(1),
            strat.calculate(2),
            strat.calculate(3),
        ]

    def test_delays_respects_cap(self):
        strat = ExponentialBackoff(
            base_delay=10.0, multiplier=2.0, max_delay=25.0, jitter=False
        )
        assert strat.delays(4) == [10.0, 20.0, 25.0, 25.0]

    def test_delays_zero_returns_empty(self):
        strat = ConstantBackoff(delay=5.0, jitter=False)
        assert strat.delays(0) == []

    def test_stateful_strategy_advances_state_once_per_element(self):
        # Decorrelated jitter is stateful: delays(n) must advance its internal
        # state n times, exactly as calling calculate() n times would.
        strat = DecorrelatedJitterBackoff(base_delay=1.0, max_delay=100.0)
        seq = strat.delays(5)
        assert len(seq) == 5
        assert seq[0] == 1.0  # first element resets to base_delay
        # State advanced: the strategy now continues from the last delay it yielded.
        assert strat._previous_delay == seq[-1]
