"""
Unit tests for backoff calculators.
"""

import pytest


class TestExponentialBackoff:
    """Tests for ExponentialBackoff calculator."""

    def test_basic_calculation(self):
        from baldur.core.backoff import ExponentialBackoff

        backoff = ExponentialBackoff(base_delay=1.0, jitter=False)

        assert backoff.calculate(1) == 1.0
        assert backoff.calculate(2) == 2.0
        assert backoff.calculate(3) == 4.0
        assert backoff.calculate(4) == 8.0

    def test_respects_max_delay(self):
        from baldur.core.backoff import ExponentialBackoff

        backoff = ExponentialBackoff(base_delay=1.0, max_delay=10.0, jitter=False)

        assert backoff.calculate(5) == 10.0  # Would be 16, capped at 10
        assert backoff.calculate(10) == 10.0

    def test_jitter_adds_randomness(self):
        from baldur.core.backoff import ExponentialBackoff

        backoff = ExponentialBackoff(base_delay=10.0, jitter=True)

        # With jitter, results should vary
        results = [backoff.calculate(2) for _ in range(10)]

        # Not all should be exactly 20.0
        assert len(set(results)) > 1

    def test_custom_multiplier(self):
        from baldur.core.backoff import ExponentialBackoff

        backoff = ExponentialBackoff(base_delay=1.0, multiplier=3.0, jitter=False)

        assert backoff.calculate(1) == 1.0
        assert backoff.calculate(2) == 3.0
        assert backoff.calculate(3) == 9.0


class TestLinearBackoff:
    """Tests for LinearBackoff calculator."""

    def test_basic_calculation(self):
        from baldur.core.backoff import LinearBackoff

        backoff = LinearBackoff(base_delay=1.0, increment=2.0, jitter=False)

        assert backoff.calculate(1) == 1.0
        assert backoff.calculate(2) == 3.0
        assert backoff.calculate(3) == 5.0

    def test_respects_max_delay(self):
        from baldur.core.backoff import LinearBackoff

        backoff = LinearBackoff(
            base_delay=1.0, increment=10.0, max_delay=25.0, jitter=False
        )

        assert backoff.calculate(3) == 21.0
        assert backoff.calculate(4) == 25.0  # Capped


class TestConstantBackoff:
    """Tests for ConstantBackoff calculator."""

    def test_constant_delay(self):
        from baldur.core.backoff import ConstantBackoff

        backoff = ConstantBackoff(delay=5.0, jitter=False)

        assert backoff.calculate(1) == 5.0
        assert backoff.calculate(10) == 5.0
        assert backoff.calculate(100) == 5.0


class TestDecorrelatedJitterBackoff:
    """Tests for DecorrelatedJitterBackoff calculator."""

    def test_first_attempt_uses_base_delay(self):
        from baldur.core.backoff import DecorrelatedJitterBackoff

        backoff = DecorrelatedJitterBackoff(base_delay=1.0)

        assert backoff.calculate(1) == 1.0

    def test_subsequent_attempts_have_jitter(self):
        from baldur.core.backoff import DecorrelatedJitterBackoff

        backoff = DecorrelatedJitterBackoff(base_delay=1.0)

        backoff.calculate(1)  # Initialize
        results = [backoff.calculate(2) for _ in range(10)]

        # With jitter, we expect variation
        # Each result depends on previous, so reset for each
        backoff.reset()

        results = []
        for _ in range(5):
            backoff.reset()
            backoff.calculate(1)
            results.append(backoff.calculate(2))

        # Should have some variation
        assert len(set(results)) > 1

    def test_reset_clears_state(self):
        from baldur.core.backoff import DecorrelatedJitterBackoff

        backoff = DecorrelatedJitterBackoff(base_delay=1.0)

        backoff.calculate(1)
        backoff.calculate(2)  # This sets _previous_delay
        backoff.reset()

        # After reset, first attempt should be base_delay
        assert backoff.calculate(1) == 1.0


class TestGetBackoffCalculator:
    """Tests for the factory function."""

    def test_get_exponential(self):
        from baldur.core.backoff import ExponentialBackoff, get_backoff_calculator

        calc = get_backoff_calculator("exponential")
        assert isinstance(calc, ExponentialBackoff)

    def test_get_linear(self):
        from baldur.core.backoff import LinearBackoff, get_backoff_calculator

        calc = get_backoff_calculator("linear")
        assert isinstance(calc, LinearBackoff)

    def test_get_with_custom_params(self):
        from baldur.core.backoff import get_backoff_calculator

        calc = get_backoff_calculator("exponential", base_delay=2.0, max_delay=100.0)

        assert calc.base_delay == 2.0
        assert calc.max_delay == 100.0

    def test_invalid_strategy_raises(self):
        from baldur.core.backoff import get_backoff_calculator

        with pytest.raises(ValueError) as exc_info:
            get_backoff_calculator("invalid")

        assert "Unknown backoff strategy" in str(exc_info.value)
