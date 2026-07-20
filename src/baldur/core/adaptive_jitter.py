"""
Adaptive jitter calculator (Platinum SLA optimization)

Dynamically adjusts the jitter range based on system state.
Stabilizes P99 and removes unnecessary delay.

Values are overridable via JitterSettings environment variables:
- BALDUR_JITTER_ERROR_BUDGET_DANGER_THRESHOLD
- BALDUR_JITTER_ERROR_BUDGET_SAFE_THRESHOLD
- BALDUR_JITTER_LOAD_HIGH_THRESHOLD
- BALDUR_JITTER_LOAD_LOW_THRESHOLD
"""

import random

from baldur.settings.jitter import get_jitter_settings

__all__ = ["AdaptiveJitter"]


class AdaptiveJitter:
    """
    Adaptive jitter calculator

    Dynamically adjusts the jitter range based on system state:
    - Relaxed conditions: minimum jitter (fast recovery)
    - Stressed conditions: maximum jitter (thundering herd prevention)

    Usage:
        # Without state information (default range)
        jitter = AdaptiveJitter.calculate()

        # With state information
        jitter = AdaptiveJitter.calculate(
            error_budget_remaining=0.3,  # 30% left
            current_load=0.7             # 70% load
        )

        # In milliseconds
        jitter_ms = AdaptiveJitter.calculate_ms()
    """

    # Jitter range settings (seconds)
    JITTER_MIN_RELAXED: tuple[float, float] = (0, 0.05)  # Relaxed: 0~50ms
    JITTER_MIN_NORMAL: tuple[float, float] = (0.03, 0.1)  # Normal: 30~100ms
    JITTER_MIN_STRESSED: tuple[float, float] = (0.1, 0.3)  # Stressed: 100~300ms

    @classmethod
    def _get_error_budget_danger_threshold(cls) -> float:
        """Error budget danger threshold (at or below 20% -> stressed)."""
        return get_jitter_settings().error_budget_danger_threshold

    @classmethod
    def _get_error_budget_safe_threshold(cls) -> float:
        """Error budget safe threshold (at or above 50% -> relaxed)."""
        return get_jitter_settings().error_budget_safe_threshold

    @classmethod
    def _get_load_high_threshold(cls) -> float:
        """High-load threshold (at or above 80% -> stressed)."""
        return get_jitter_settings().load_high_threshold

    @classmethod
    def _get_load_low_threshold(cls) -> float:
        """Low-load threshold (at or below 30% -> relaxed)."""
        return get_jitter_settings().load_low_threshold

    @classmethod
    def calculate(
        cls,
        error_budget_remaining: float | None = None,
        current_load: float | None = None,
    ) -> float:
        """
        Calculate the jitter value for the current situation

        Args:
            error_budget_remaining: Remaining error budget ratio (0.0 ~ 1.0)
            current_load: Current system load (0.0 ~ 1.0)

        Returns:
            Jitter value to apply (seconds)
        """
        jitter_range = cls.get_jitter_range(error_budget_remaining, current_load)
        return random.uniform(*jitter_range)

    @classmethod
    def calculate_ms(
        cls,
        error_budget_remaining: float | None = None,
        current_load: float | None = None,
    ) -> int:
        """Return the value in milliseconds."""
        return int(cls.calculate(error_budget_remaining, current_load) * 1000)

    @classmethod
    def get_jitter_range(
        cls,
        error_budget_remaining: float | None = None,
        current_load: float | None = None,
    ) -> tuple[float, float]:
        """
        Determine the jitter range for the current situation

        Args:
            error_budget_remaining: Remaining error budget ratio (0.0 ~ 1.0)
            current_load: Current system load (0.0 ~ 1.0)

        Returns:
            (min_jitter, max_jitter) tuple (seconds)
        """
        # No information available -> use the normal range
        if error_budget_remaining is None and current_load is None:
            return cls.JITTER_MIN_NORMAL

        # Stressed determination (thresholds from settings)
        is_budget_danger = (
            error_budget_remaining is not None
            and error_budget_remaining < cls._get_error_budget_danger_threshold()
        )
        is_load_high = (
            current_load is not None and current_load > cls._get_load_high_threshold()
        )

        # Relaxed determination (thresholds from settings)
        is_budget_safe = (
            error_budget_remaining is not None
            and error_budget_remaining > cls._get_error_budget_safe_threshold()
        )
        is_load_low = (
            current_load is not None and current_load < cls._get_load_low_threshold()
        )

        # Stressed: maximum jitter
        if is_budget_danger or is_load_high:
            return cls.JITTER_MIN_STRESSED

        # Relaxed: minimum jitter
        if is_budget_safe and is_load_low:
            return cls.JITTER_MIN_RELAXED

        # Normal: intermediate jitter
        return cls.JITTER_MIN_NORMAL

    @classmethod
    def get_status(
        cls,
        error_budget_remaining: float | None = None,
        current_load: float | None = None,
    ) -> str:
        """
        Return the current status string

        Returns:
            'relaxed', 'normal', or 'stressed'
        """
        jitter_range = cls.get_jitter_range(error_budget_remaining, current_load)

        if jitter_range == cls.JITTER_MIN_RELAXED:
            return "relaxed"
        if jitter_range == cls.JITTER_MIN_STRESSED:
            return "stressed"
        return "normal"
