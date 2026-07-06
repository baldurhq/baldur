"""
Tests for core backoff strategies.

Unit tests for ExponentialBackoff, LinearBackoff, ConstantBackoff,
DecorrelatedJitterBackoff, and get_backoff_calculator factory function.
"""

from unittest.mock import MagicMock

import pytest

from baldur.core.backoff import (
    BackoffStrategy,
    ConstantBackoff,
    DecorrelatedJitterBackoff,
    ExponentialBackoff,
    LinearBackoff,
    get_backoff_calculator,
)

# =============================================================================
# ExponentialBackoff Tests
# =============================================================================


class TestExponentialBackoff:
    """Exponential backoff strategy tests."""

    def test_basic_exponential_growth(self):
        """Basic exponential growth — jitter disabled."""
        backoff = ExponentialBackoff(base_delay=1.0, multiplier=2.0, jitter=False)
        assert backoff.calculate(1) == 1.0  # 1 * 2^0
        assert backoff.calculate(2) == 2.0  # 1 * 2^1
        assert backoff.calculate(3) == 4.0  # 1 * 2^2
        assert backoff.calculate(4) == 8.0  # 1 * 2^3

    def test_max_delay_cap(self):
        """Delay is capped at max_delay."""
        backoff = ExponentialBackoff(
            base_delay=1.0, multiplier=10.0, max_delay=50.0, jitter=False
        )
        # attempt=3: 1 * 10^2 = 100 → capped to 50
        assert backoff.calculate(3) == 50.0

    def test_jitter_within_range(self):
        """Jitter stays within ±jitter_factor of base delay."""
        backoff = ExponentialBackoff(
            base_delay=10.0,
            multiplier=2.0,
            max_delay=300.0,
            jitter=True,
            jitter_factor=0.2,
        )
        for _ in range(100):
            delay = backoff.calculate(1)
            # base_delay=10, jitter_factor=0.2 → 10 ± 2 → [8, 12]
            assert 8.0 <= delay <= 12.0

    def test_jitter_non_negative(self):
        """Jitter never produces negative delays."""
        backoff = ExponentialBackoff(
            base_delay=0.1,
            multiplier=1.0,
            jitter=True,
            jitter_factor=0.99,
        )
        for _ in range(100):
            delay = backoff.calculate(1)
            assert delay >= 0.0

    def test_reset_is_noop(self):
        """Reset is a no-op for stateless strategy."""
        backoff = ExponentialBackoff()
        backoff.reset()  # no exception

    def test_from_settings(self):
        """Factory creates instance from settings object."""
        mock_settings = MagicMock()
        mock_settings.exponential_base_delay = 2.0
        mock_settings.exponential_max_delay = 120.0
        mock_settings.exponential_multiplier = 3.0
        mock_settings.exponential_jitter_factor = 0.1
        backoff = ExponentialBackoff.from_settings(settings=mock_settings)
        assert backoff.base_delay == 2.0
        assert backoff.max_delay == 120.0
        assert backoff.multiplier == 3.0

    def test_from_settings_with_overrides(self):
        """Override params take precedence over settings."""
        mock_settings = MagicMock()
        mock_settings.exponential_base_delay = 2.0
        mock_settings.exponential_max_delay = 120.0
        mock_settings.exponential_multiplier = 3.0
        mock_settings.exponential_jitter_factor = 0.1
        backoff = ExponentialBackoff.from_settings(
            settings=mock_settings, base_delay=5.0
        )
        assert backoff.base_delay == 5.0  # overridden
        assert backoff.max_delay == 120.0  # from settings


# =============================================================================
# LinearBackoff Tests
# =============================================================================


class TestLinearBackoff:
    """Linear backoff strategy tests."""

    def test_basic_linear_growth(self):
        """Delay grows by fixed increment per attempt."""
        backoff = LinearBackoff(base_delay=1.0, increment=2.0, jitter=False)
        assert backoff.calculate(1) == 1.0  # 1 + 2*0
        assert backoff.calculate(2) == 3.0  # 1 + 2*1
        assert backoff.calculate(3) == 5.0  # 1 + 2*2

    def test_max_delay_cap(self):
        """Delay is capped at max_delay."""
        backoff = LinearBackoff(
            base_delay=1.0, increment=100.0, max_delay=50.0, jitter=False
        )
        assert backoff.calculate(2) == 50.0  # 1 + 100*1 = 101 → capped

    def test_with_jitter(self):
        """Jitter stays within expected range."""
        backoff = LinearBackoff(
            base_delay=10.0,
            increment=0.0,
            max_delay=100.0,
            jitter=True,
            jitter_factor=0.1,
        )
        for _ in range(100):
            delay = backoff.calculate(1)
            assert 9.0 <= delay <= 11.0

    def test_from_settings(self):
        """Factory creates instance from settings object."""
        mock_settings = MagicMock()
        mock_settings.linear_base_delay = 2.0
        mock_settings.linear_increment = 1.5
        mock_settings.linear_max_delay = 60.0
        mock_settings.linear_jitter_factor = 0.1
        backoff = LinearBackoff.from_settings(settings=mock_settings)
        assert backoff.base_delay == 2.0
        assert backoff.increment == 1.5


# =============================================================================
# ConstantBackoff Tests
# =============================================================================


class TestConstantBackoff:
    """Constant backoff strategy tests."""

    def test_constant_delay(self):
        """Same delay regardless of attempt number."""
        backoff = ConstantBackoff(delay=5.0, jitter=False)
        assert backoff.calculate(1) == 5.0
        assert backoff.calculate(2) == 5.0
        assert backoff.calculate(100) == 5.0

    def test_with_jitter(self):
        """Jitter stays within expected range."""
        backoff = ConstantBackoff(delay=10.0, jitter=True, jitter_factor=0.1)
        for _ in range(100):
            delay = backoff.calculate(1)
            assert 9.0 <= delay <= 11.0

    def test_from_settings(self):
        """Factory creates instance from settings object."""
        mock_settings = MagicMock()
        mock_settings.constant_delay = 7.0
        mock_settings.constant_jitter_factor = 0.05
        backoff = ConstantBackoff.from_settings(settings=mock_settings)
        assert backoff.delay == 7.0


# =============================================================================
# DecorrelatedJitterBackoff Tests
# =============================================================================


class TestDecorrelatedJitterBackoff:
    """Decorrelated jitter backoff (AWS-style) tests."""

    def test_first_attempt_returns_base(self):
        """First attempt returns base_delay."""
        backoff = DecorrelatedJitterBackoff(base_delay=1.0, max_delay=300.0)
        assert backoff.calculate(1) == 1.0

    def test_subsequent_attempts_use_previous(self):
        """Later attempts derive from previous delay."""
        backoff = DecorrelatedJitterBackoff(base_delay=1.0, max_delay=300.0)
        backoff.calculate(1)  # sets _previous_delay = 1.0
        delay2 = backoff.calculate(2)
        # delay2 in [1.0, 3.0] (base_delay ~ previous*3)
        assert 1.0 <= delay2 <= 3.0

    def test_max_delay_cap(self):
        """Delay is capped at max_delay."""
        backoff = DecorrelatedJitterBackoff(base_delay=100.0, max_delay=200.0)
        backoff.calculate(1)  # _previous_delay = 100
        for _ in range(100):
            d = backoff.calculate(2)
            assert d <= 200.0

    def test_reset_clears_previous(self):
        """Reset starts a fresh sequence."""
        backoff = DecorrelatedJitterBackoff(base_delay=1.0, max_delay=300.0)
        backoff.calculate(1)
        backoff.calculate(2)
        backoff.reset()
        # after reset, attempt=1 returns base_delay again
        assert backoff.calculate(1) == 1.0

    def test_from_settings(self):
        """Factory creates instance from settings object."""
        mock_settings = MagicMock()
        mock_settings.decorrelated_base_delay = 2.0
        mock_settings.decorrelated_max_delay = 150.0
        backoff = DecorrelatedJitterBackoff.from_settings(settings=mock_settings)
        assert backoff.base_delay == 2.0
        assert backoff.max_delay == 150.0


# =============================================================================
# Factory Function Tests
# =============================================================================


class TestGetBackoffCalculator:
    """get_backoff_calculator factory function tests."""

    def test_default_strategy_is_exponential(self):
        """No-arg call defaults to the 'exponential' strategy."""
        calc = get_backoff_calculator()
        assert isinstance(calc, ExponentialBackoff)

    def test_create_exponential(self):
        """Creates ExponentialBackoff for 'exponential' strategy."""
        calc = get_backoff_calculator("exponential", base_delay=2.0)
        assert isinstance(calc, ExponentialBackoff)
        assert calc.base_delay == 2.0

    def test_create_linear(self):
        """Creates LinearBackoff for 'linear' strategy."""
        calc = get_backoff_calculator("linear", base_delay=1.0, increment=3.0)
        assert isinstance(calc, LinearBackoff)

    def test_create_constant(self):
        """Creates ConstantBackoff for 'constant' strategy."""
        calc = get_backoff_calculator("constant", delay=5.0)
        assert isinstance(calc, ConstantBackoff)

    def test_create_decorrelated(self):
        """Creates DecorrelatedJitterBackoff for 'decorrelated' strategy."""
        calc = get_backoff_calculator("decorrelated", base_delay=1.0)
        assert isinstance(calc, DecorrelatedJitterBackoff)

    def test_unknown_strategy_raises(self):
        """Unknown strategy name raises ValueError."""
        with pytest.raises(ValueError, match="Unknown backoff strategy"):
            get_backoff_calculator("unknown_strategy")


# =============================================================================
# Abstract BackoffStrategy Interface Tests
# =============================================================================


class TestBackoffStrategyInterface:
    """BackoffStrategy abstract class enforcement."""

    def test_cannot_instantiate_abstract(self):
        """Cannot instantiate abstract class directly."""
        with pytest.raises(TypeError):
            BackoffStrategy()
