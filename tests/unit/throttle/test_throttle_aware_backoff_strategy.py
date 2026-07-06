"""
ThrottleAwareBackoffStrategy adapter unit tests.

Test target: services/backoff_calculator/strategy_adapter.py
- BackoffStrategy interface compliance
- Delegation to ThrottleAwareBackoffCalculator
- reset() no-op behavior

Reference:
    docs/baldur/middleware_system/310_FUNCTIONAL_DUPLICATION_ELIMINATION.md §3.1.6.2
"""

from __future__ import annotations

from unittest.mock import MagicMock

from baldur.core.backoff import BackoffStrategy
from baldur.services.backoff_calculator.strategy_adapter import (
    ThrottleAwareBackoffStrategy,
)

# =============================================================================
# Contract Tests
# =============================================================================


class TestThrottleAwareBackoffStrategyContract:
    """ThrottleAwareBackoffStrategy interface contract verification."""

    def test_is_instance_of_backoff_strategy(self):
        """ThrottleAwareBackoffStrategy is a BackoffStrategy subclass."""
        calculator = MagicMock()
        calculator.calculate_with_throttle_context.return_value = (5.0, 1.0, "normal")
        strategy = ThrottleAwareBackoffStrategy(calculator)
        assert isinstance(strategy, BackoffStrategy)

    def test_calculate_returns_float(self):
        """calculate() returns a float value."""
        calculator = MagicMock()
        calculator.calculate_with_throttle_context.return_value = (
            10,
            2.0,
            "sla_warning",
        )
        strategy = ThrottleAwareBackoffStrategy(calculator)
        result = strategy.calculate(attempt=1)
        assert isinstance(result, float)


# =============================================================================
# Behavior Tests
# =============================================================================


class TestThrottleAwareBackoffStrategyBehavior:
    """ThrottleAwareBackoffStrategy delegation and behavior verification."""

    def test_delegates_to_calculate_with_throttle_context(self):
        """calculate() delegates to calculator.calculate_with_throttle_context()."""
        calculator = MagicMock()
        calculator.calculate_with_throttle_context.return_value = (
            8.0,
            1.5,
            "sla_warning",
        )
        strategy = ThrottleAwareBackoffStrategy(calculator)

        result = strategy.calculate(attempt=3)

        calculator.calculate_with_throttle_context.assert_called_once_with(
            3, with_jitter=True
        )
        assert result == 8.0

    def test_returns_delay_from_calculator_tuple(self):
        """calculate() returns only the delay (first element) from the 3-tuple."""
        calculator = MagicMock()
        calculator.calculate_with_throttle_context.return_value = (
            42,
            4.0,
            "emergency_3",
        )
        strategy = ThrottleAwareBackoffStrategy(calculator)

        result = strategy.calculate(attempt=2)

        assert result == 42.0

    def test_negative_delay_signals_full_stop(self):
        """Negative delay from calculator is passed through (Full Stop signal)."""
        calculator = MagicMock()
        calculator.calculate_with_throttle_context.return_value = (
            -1,
            float("inf"),
            "full_stop_active",
        )
        strategy = ThrottleAwareBackoffStrategy(calculator)

        result = strategy.calculate(attempt=1)

        assert result == -1.0

    def test_reset_is_noop(self):
        """reset() does not raise and is effectively a no-op."""
        calculator = MagicMock()
        strategy = ThrottleAwareBackoffStrategy(calculator)
        strategy.reset()  # Should not raise

    def test_context_parameter_accepted(self):
        """calculate() accepts optional context parameter."""
        calculator = MagicMock()
        calculator.calculate_with_throttle_context.return_value = (5.0, 1.0, "normal")
        strategy = ThrottleAwareBackoffStrategy(calculator)

        mock_context = MagicMock()
        result = strategy.calculate(attempt=1, context=mock_context)

        assert result == 5.0
