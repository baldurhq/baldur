"""
Adapter: ThrottleAwareBackoffCalculator as BackoffStrategy.

Bridges the services/backoff_calculator layer to the core/backoff.py
BackoffStrategy interface, enabling retry_with_backoff() to use
throttle-aware backoff without coupling to the calculator's API.

Usage:
    from baldur.services.backoff_calculator.strategy_adapter import (
        ThrottleAwareBackoffStrategy,
    )
    from baldur.services.backoff_calculator import ThrottleAwareBackoffCalculator

    calculator = ThrottleAwareBackoffCalculator(config, service_name="payment")
    strategy = ThrottleAwareBackoffStrategy(calculator)

    # Use with retry_with_backoff(). Note this is the retry *primitive's*
    # config, which is a different class from the policy's RetryPolicyConfig.
    from baldur.core.retry import RetryConfig

    config = RetryConfig(backoff=strategy)
    outcome = retry_with_backoff(func, config)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from baldur.core.backoff import BackoffStrategy

if TYPE_CHECKING:
    from baldur.interfaces.resilience_policy import PolicyContext

    from .calculator import ThrottleAwareBackoffCalculator

__all__ = ["ThrottleAwareBackoffStrategy"]


class ThrottleAwareBackoffStrategy(BackoffStrategy):
    """Adapter: expose ThrottleAwareBackoffCalculator as BackoffStrategy.

    Delegates calculate() to the underlying calculator, making
    throttle-aware backoff usable anywhere a BackoffStrategy is expected
    (e.g. retry_with_backoff, RetryPolicy).
    """

    def __init__(self, calculator: ThrottleAwareBackoffCalculator) -> None:
        self._calculator = calculator

    def calculate(self, attempt: int, context: PolicyContext | None = None) -> float:
        """Calculate throttle-aware backoff delay.

        Uses calculate_with_throttle_context() when available to incorporate
        system load state. Falls back to basic calculate() otherwise.

        Returns:
            Delay in seconds. A negative value signals Full Stop
            (immediate DLQ routing).
        """
        delay, _multiplier, _reason = self._calculator.calculate_with_throttle_context(
            attempt, with_jitter=True
        )
        return float(delay)

    def reset(self) -> None:
        """Reset is a no-op — calculator state is managed externally."""
