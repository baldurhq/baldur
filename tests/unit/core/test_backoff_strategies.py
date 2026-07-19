"""
Tests for core backoff strategies.

Unit tests for ExponentialBackoff, LinearBackoff, ConstantBackoff,
DecorrelatedJitterBackoff, and get_backoff_calculator factory function.
"""

from unittest.mock import MagicMock

import pytest
from hypothesis import given
from hypothesis import settings as hyp_settings
from hypothesis import strategies as st

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


# =============================================================================
# Hard delay cap (jitter can no longer exceed max_delay)
# =============================================================================


class TestBackoffHardCapBehavior:
    """Jittered Exponential/Linear delays never exceed ``max_delay``.

    Before this contract, jitter was applied *after* the ``min(delay, max_delay)``
    clamp, so the effective delay could overshoot the documented cap by up to
    ``jitter_factor``. The cap is now a hard ceiling on the returned value.
    """

    # A saturating config: base * multiplier ** (attempt-1) blows past max_delay
    # by attempt 4, so attempts >= 4 exercise the at-saturation jitter regime and
    # attempt 1 (base_delay well below the cap) exercises the below-saturation one.
    _CAP = 60.0
    _JITTER_FACTOR = 0.3

    @given(
        base=st.floats(min_value=0.1, max_value=100.0),
        multiplier=st.floats(min_value=1.1, max_value=5.0),
        max_delay=st.floats(min_value=1.0, max_value=300.0),
        jitter_factor=st.floats(min_value=0.0, max_value=1.0),
        attempt=st.integers(min_value=1, max_value=20),
    )
    @hyp_settings(max_examples=300)
    def test_exponential_delay_never_exceeds_max_delay(
        self, base, multiplier, max_delay, jitter_factor, attempt
    ):
        """No (config, attempt, jitter draw) yields a delay above max_delay or below 0."""
        backoff = ExponentialBackoff(
            base_delay=base,
            multiplier=multiplier,
            max_delay=max_delay,
            jitter=True,
            jitter_factor=jitter_factor,
        )
        delay = backoff.calculate(attempt)
        assert 0.0 <= delay <= max_delay

    @given(
        base=st.floats(min_value=0.1, max_value=100.0),
        increment=st.floats(min_value=0.0, max_value=100.0),
        max_delay=st.floats(min_value=1.0, max_value=300.0),
        jitter_factor=st.floats(min_value=0.0, max_value=1.0),
        attempt=st.integers(min_value=1, max_value=20),
    )
    @hyp_settings(max_examples=300)
    def test_linear_delay_never_exceeds_max_delay(
        self, base, increment, max_delay, jitter_factor, attempt
    ):
        """Linear shares the same hard-cap shape as exponential."""
        backoff = LinearBackoff(
            base_delay=base,
            increment=increment,
            max_delay=max_delay,
            jitter=True,
            jitter_factor=jitter_factor,
        )
        delay = backoff.calculate(attempt)
        assert 0.0 <= delay <= max_delay

    def test_saturated_jitter_is_inward_only(self):
        """At saturation the jitter draws inward: ``[max_delay - width, max_delay]``."""
        backoff = ExponentialBackoff(
            base_delay=1000.0,  # attempt 1 already saturates a 60s cap
            multiplier=2.0,
            max_delay=self._CAP,
            jitter=True,
            jitter_factor=self._JITTER_FACTOR,
        )
        lower_bound = self._CAP - self._CAP * self._JITTER_FACTOR
        for _ in range(500):
            delay = backoff.calculate(1)
            assert lower_bound <= delay <= self._CAP

    def test_saturated_jitter_preserves_dispersion_width(self):
        """Inward jitter keeps the full dispersion band — it does not collapse to the cap."""
        backoff = ExponentialBackoff(
            base_delay=1000.0,
            multiplier=2.0,
            max_delay=self._CAP,
            jitter=True,
            jitter_factor=self._JITTER_FACTOR,
        )
        samples = [backoff.calculate(1) for _ in range(500)]
        # Over 500 draws the minimum should fall well below the cap (dispersion
        # preserved), not sit pinned at max_delay.
        assert min(samples) < self._CAP - (self._CAP * self._JITTER_FACTOR) / 2

    def test_below_saturation_jitter_stays_under_cap(self):
        """Below saturation, symmetric jitter around a near-cap raw value still clamps to the cap."""
        # raw = 55, jitter_factor 0.3 → symmetric band [38.5, 71.5], but capped at 60.
        backoff = ExponentialBackoff(
            base_delay=55.0,
            multiplier=2.0,
            max_delay=self._CAP,
            jitter=True,
            jitter_factor=self._JITTER_FACTOR,
        )
        saw_above_raw = False
        for _ in range(500):
            delay = backoff.calculate(1)
            assert delay <= self._CAP
            if delay > 55.0:
                saw_above_raw = True
        # The upward half of the symmetric jitter is exercised (not one-sided).
        assert saw_above_raw

    def test_no_jitter_clamps_without_exceeding_cap(self):
        """With jitter off the value is a plain clamp into ``[0, max_delay]``."""
        backoff = ExponentialBackoff(
            base_delay=10.0,
            multiplier=2.0,
            max_delay=self._CAP,
            jitter=False,
        )
        assert backoff.calculate(1) == 10.0
        assert backoff.calculate(10) == self._CAP  # saturated, no jitter
