"""
Backoff calculation strategies for retry mechanisms.

This module provides various backoff strategies for calculating
delay between retry attempts.
"""

from __future__ import annotations

import random
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from baldur.interfaces.resilience_policy import PolicyContext


class BackoffStrategy(ABC):
    """Abstract base class for backoff calculation strategies."""

    @abstractmethod
    def calculate(self, attempt: int, context: PolicyContext | None = None) -> float:
        """
        Calculate the delay for the given attempt number.

        Contract: ``attempt`` is **1-indexed** — the first retry passes
        ``attempt=1``, so every strategy yields ``base_delay`` (or its constant
        equivalent) on the first retry: exponential uses ``multiplier ** (attempt
        - 1)``, linear uses ``increment * (attempt - 1)``, and decorrelated resets
        at ``attempt == 1``. A 0-indexed caller must add 1 before calling, or the
        first retry fires hotter than configured (``base_delay / multiplier``).

        Args:
            attempt: The current attempt number (1-indexed; first retry is 1)
            context: Policy execution context (tier_id, domain, etc.)

        Returns:
            The delay in seconds before the next retry
        """
        pass

    @abstractmethod
    def reset(self) -> None:
        """Reset the backoff calculator to its initial state."""
        pass

    def delays(self, n: int) -> list[float]:
        """Return the first ``n`` retry delays as a list.

        Convenience over :meth:`calculate` for callers that need the whole
        interval schedule up front (e.g. an RQ ``Retry(interval=[...])`` list).
        Delays are 1-indexed like :meth:`calculate`, so ``delays(3)`` returns the
        delays for attempts 1, 2 and 3.

        Note:
            Stateful strategies (e.g. decorrelated jitter) advance their internal
            state once per element — identical to calling :meth:`calculate` ``n``
            times in sequence.
        """
        return [self.calculate(i) for i in range(1, n + 1)]


@dataclass
class ExponentialBackoff(BackoffStrategy):
    """
    Exponential backoff strategy.

    Delay grows exponentially with each attempt: base_delay * (multiplier ^ attempt)
    Optional jitter adds randomness to prevent thundering herd.
    """

    base_delay: float = 1.0
    # Standard max delay cap of 1 minute, aligned with the CB recovery_timeout
    # default and the shared STANDARD_MAX_DELAY used by from_settings().
    max_delay: float = 60.0
    multiplier: float = 2.0
    jitter: bool = True
    jitter_factor: float = 0.2

    @classmethod
    def from_settings(cls, settings=None, **overrides) -> ExponentialBackoff:
        """
        Create an instance from settings.

        Args:
            settings: BackoffSettings instance (auto-loaded when None)
            **overrides: per-field overrides

        Returns:
            ExponentialBackoff: settings-derived instance
        """
        from baldur.settings.backoff import get_backoff_settings

        s = settings or get_backoff_settings()
        return cls(
            base_delay=overrides.get("base_delay", s.exponential_base_delay),
            max_delay=overrides.get("max_delay", s.exponential_max_delay),
            multiplier=overrides.get("multiplier", s.exponential_multiplier),
            jitter=overrides.get("jitter", True),
            jitter_factor=overrides.get("jitter_factor", s.exponential_jitter_factor),
        )

    def calculate(self, attempt: int, context: PolicyContext | None = None) -> float:
        """Calculate exponential delay with optional jitter."""
        delay = self.base_delay * (self.multiplier ** (attempt - 1))
        delay = min(delay, self.max_delay)

        if self.jitter:
            jitter_range = delay * self.jitter_factor
            delay = delay + random.uniform(-jitter_range, jitter_range)
            delay = max(0.0, delay)

        return delay

    def reset(self) -> None:
        """Reset is a no-op for stateless exponential backoff."""
        pass


@dataclass
class LinearBackoff(BackoffStrategy):
    """
    Linear backoff strategy.

    Delay grows linearly with each attempt: base_delay + (increment * attempt)
    """

    base_delay: float = 1.0
    increment: float = 1.0
    max_delay: float = 60.0
    jitter: bool = False
    jitter_factor: float = 0.1

    @classmethod
    def from_settings(cls, settings=None, **overrides) -> LinearBackoff:
        """
        Create an instance from settings.

        Args:
            settings: BackoffSettings instance (auto-loaded when None)
            **overrides: per-field overrides

        Returns:
            LinearBackoff: settings-derived instance
        """
        from baldur.settings.backoff import get_backoff_settings

        s = settings or get_backoff_settings()
        return cls(
            base_delay=overrides.get("base_delay", s.linear_base_delay),
            increment=overrides.get("increment", s.linear_increment),
            max_delay=overrides.get("max_delay", s.linear_max_delay),
            jitter=overrides.get("jitter", False),
            jitter_factor=overrides.get("jitter_factor", s.linear_jitter_factor),
        )

    def calculate(self, attempt: int, context: PolicyContext | None = None) -> float:
        """Calculate linear delay."""
        delay = self.base_delay + (self.increment * (attempt - 1))
        delay = min(delay, self.max_delay)

        if self.jitter:
            jitter_range = delay * self.jitter_factor
            delay = delay + random.uniform(-jitter_range, jitter_range)
            delay = max(0.0, delay)

        return delay

    def reset(self) -> None:
        """Reset is a no-op for stateless linear backoff."""
        pass


@dataclass
class ConstantBackoff(BackoffStrategy):
    """
    Constant backoff strategy.

    Delay is constant regardless of attempt number.
    """

    delay: float = 5.0
    jitter: bool = False
    jitter_factor: float = 0.1

    @classmethod
    def from_settings(cls, settings=None, **overrides) -> ConstantBackoff:
        """
        Create an instance from settings.

        Args:
            settings: BackoffSettings instance (auto-loaded when None)
            **overrides: per-field overrides

        Returns:
            ConstantBackoff: settings-derived instance
        """
        from baldur.settings.backoff import get_backoff_settings

        s = settings or get_backoff_settings()
        return cls(
            delay=overrides.get("delay", s.constant_delay),
            jitter=overrides.get("jitter", False),
            jitter_factor=overrides.get("jitter_factor", s.constant_jitter_factor),
        )

    def calculate(self, attempt: int, context: PolicyContext | None = None) -> float:
        """Return constant delay."""
        result = self.delay

        if self.jitter:
            jitter_range = result * self.jitter_factor
            result = result + random.uniform(-jitter_range, jitter_range)
            result = max(0.0, result)

        return result

    def reset(self) -> None:
        """Reset is a no-op for constant backoff."""
        pass


@dataclass
class DecorrelatedJitterBackoff(BackoffStrategy):
    """
    Decorrelated jitter backoff strategy (AWS-style).

    Each delay is randomly chosen between base_delay and 3 * previous_delay.
    This provides better distribution than simple exponential with jitter.
    """

    base_delay: float = 1.0
    # Standard max delay cap of 1 minute, matching the shared STANDARD_MAX_DELAY
    # used by from_settings() (keeps direct construction and settings consistent).
    max_delay: float = 60.0
    _previous_delay: float | None = None

    @classmethod
    def from_settings(cls, settings=None, **overrides) -> DecorrelatedJitterBackoff:
        """
        Create an instance from settings.

        Args:
            settings: BackoffSettings instance (auto-loaded when None)
            **overrides: per-field overrides

        Returns:
            DecorrelatedJitterBackoff: settings-derived instance
        """
        from baldur.settings.backoff import get_backoff_settings

        s = settings or get_backoff_settings()
        return cls(
            base_delay=overrides.get("base_delay", s.decorrelated_base_delay),
            max_delay=overrides.get("max_delay", s.decorrelated_max_delay),
        )

    def calculate(self, attempt: int, context: PolicyContext | None = None) -> float:
        """Calculate decorrelated jitter delay."""
        if self._previous_delay is None or attempt == 1:
            delay = self.base_delay
        else:
            delay = random.uniform(self.base_delay, self._previous_delay * 3)

        delay = min(delay, self.max_delay)
        self._previous_delay = delay
        return delay

    def reset(self) -> None:
        """Reset the previous delay tracking."""
        self._previous_delay = None


def get_backoff_calculator(strategy: str = "exponential", **kwargs) -> BackoffStrategy:
    """
    Factory function to create a backoff calculator.

    Args:
        strategy: One of 'exponential', 'linear', 'constant', 'decorrelated'
        **kwargs: Strategy-specific parameters

    Returns:
        A BackoffStrategy instance

    Raises:
        ValueError: If an unknown strategy is specified
    """
    strategies = {
        "exponential": ExponentialBackoff,
        "linear": LinearBackoff,
        "constant": ConstantBackoff,
        "decorrelated": DecorrelatedJitterBackoff,
    }

    if strategy not in strategies:
        raise ValueError(
            f"Unknown backoff strategy: {strategy}. "
            f"Available: {list(strategies.keys())}"
        )

    return strategies[strategy](**kwargs)
