"""
Time-series forecasters: HoltLinearForecaster, EWMAForecaster, HoltWintersForecaster.

HoltLinearForecaster (double exponential smoothing):
    Tracks level and trend separately to predict the direction of a
    metric 5-15 minutes ahead.
    Two parameters (alpha, beta), O(1) state, no external dependencies.

EWMAForecaster (exponentially weighted moving average):
    Utility dedicated to noise removal / smoothing preprocessing.
    Not used for standalone prediction (horizontal forecasts only).

HoltWintersForecaster (triple exponential smoothing):
    Detects and forecasts seasonality patterns.
    Activates after sufficient history (2-3 seasons) has accumulated.

Usage:
    from baldur.core.time_series import (
        HoltLinearForecaster,
        EWMAForecaster,
        ForecastDataPoint,
    )

    forecaster = HoltLinearForecaster(alpha=0.3, beta=0.1)
    for value in metric_values:
        forecaster.update(value)
    predicted = forecaster.predict(steps_ahead=5)
"""

from __future__ import annotations

import collections
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import structlog

from baldur.utils.time import utc_now

logger = structlog.get_logger()

__all__ = [
    "EWMAForecaster",
    "ForecastDataPoint",
    "HoltLinearForecaster",
    "HoltWintersForecaster",
]


# =============================================================================
# Data models
# =============================================================================


@dataclass
class ForecastDataPoint:
    """
    Individual data point in the forecast history.

    The has_adjustment flag is a marker for Self-Fulfilling Prophecy
    prevention. Metrics observed right after a self-healing intervention
    (adjustment) reflect artificial variation rather than a natural trend,
    so the flag serves as the basis for future filtering / weight tuning.
    Currently it is record-only; differential weighting is a future
    consideration.
    """

    value: float
    timestamp: datetime = field(default_factory=lambda: utc_now())
    has_adjustment: bool = False


# =============================================================================
# HoltLinearForecaster (double exponential smoothing)
# =============================================================================


class HoltLinearForecaster:
    """
    Holt's Linear (Double Exponential Smoothing) forecaster.

    While EWMAForecaster can only produce horizontal forecasts,
    HoltLinearForecaster tracks level and trend separately to provide
    directional predictions.

    Implemented with the standard library only — no external dependencies.
    Two parameters (alpha, beta), O(1) state, bounded memory.

    Formulas::

        level_t = alpha * value_t + (1 - alpha) * (level_{t-1} + trend_{t-1})
        trend_t = beta * (level_t - level_{t-1}) + (1 - beta) * trend_{t-1}
        forecast_{t+h} = level_t + h * trend_t

    BudgetDepletionForecaster uses linear extrapolation;
    HoltLinearForecaster is its natural extension via exponential
    smoothing. Same architecture as the preemptive-protection pattern in
    AdaptiveThrottle._check_preemptive_protection().

    Args:
        alpha: Level smoothing factor (0 < alpha <= 1). Default 0.3.
        beta: Trend smoothing factor (0 < beta <= 1). Default 0.1.
            Smaller values are conservative toward trend changes
            (0.01-0.05: long-term trend), larger values are sensitive
            (0.1-0.3: short-term trend).
        warmup_samples: Minimum data points (confidence=0 below this).
        max_history: Maximum ring buffer size.
    """

    STORAGE_KEY_PREFIX = "forecaster"

    def __init__(
        self,
        alpha: float = 0.3,
        beta: float = 0.1,
        warmup_samples: int = 30,
        max_history: int = 10_000,
    ):
        if not (0 < alpha <= 1):
            raise ValueError(f"alpha must be in (0, 1]: {alpha}")
        if not (0 < beta <= 1):
            raise ValueError(f"beta must be in (0, 1]: {beta}")

        self._alpha = alpha
        self._beta = beta
        self._warmup_samples = warmup_samples

        self._level: float | None = None
        self._trend: float = 0.0
        self._count: int = 0

        self._history: collections.deque[ForecastDataPoint] = collections.deque(
            maxlen=max_history
        )

        # Phase 3: differential weighting for has_adjustment (Option A).
        # Temporarily lower alpha for 3-5 steps right after a self-healing
        # intervention to reduce the impact of artificial variation and
        # prevent trend distortion.
        self._adjustment_cooldown: int = 0
        self._adjustment_cooldown_steps: int = (
            3  # conservative steps after intervention
        )
        self._adjustment_alpha_ratio: float = 0.5  # alpha reduction during cooldown

    @property
    def is_warmed_up(self) -> bool:
        """Whether at least warmup_samples data points have accumulated."""
        return self._count >= self._warmup_samples

    @property
    def count(self) -> int:
        """Number of data points collected so far."""
        return self._count

    def update(self, value: float, has_adjustment: bool = False) -> float:
        """
        Add a new data point and return the current level.

        Args:
            value: Observed value.
            has_adjustment: Whether a self-healing intervention happened
                right before this observation. Phase 3 (Option A): when
                True, alpha is lowered for the next 3 steps to reduce
                the impact of artificial variation.

        Returns:
            Current smoothed level.
        """
        self._count += 1
        self._history.append(
            ForecastDataPoint(value=value, has_adjustment=has_adjustment)
        )

        # Phase 3: has_adjustment starts the cooldown
        if has_adjustment:
            self._adjustment_cooldown = self._adjustment_cooldown_steps

        # During cooldown, temporarily lower alpha (Option A)
        if self._adjustment_cooldown > 0:
            effective_alpha = self._alpha * self._adjustment_alpha_ratio
            self._adjustment_cooldown -= 1
        else:
            effective_alpha = self._alpha

        if self._level is None:
            self._level = value
            self._trend = 0.0
        else:
            prev_level = self._level
            self._level = effective_alpha * value + (1 - effective_alpha) * (
                prev_level + self._trend
            )
            self._trend = (
                self._beta * (self._level - prev_level) + (1 - self._beta) * self._trend
            )

        return self._level

    def predict(self, steps_ahead: int = 5) -> float | None:
        """
        Return the forecast N steps ahead.

        Returns None below warmup_samples (Cold Start protection).

        Args:
            steps_ahead: Number of future steps to forecast.

        Returns:
            Forecast value, or None when data is insufficient.
        """
        if self._level is None or not self.is_warmed_up:
            return None
        return self._level + steps_ahead * self._trend

    def get_confidence(self) -> float:
        """
        Current forecast confidence (0.0 - 1.0).

        Returns 0.0 below warmup_samples. Grows proportionally with the
        amount of data afterwards, reaching 1.0 at 200+ points.

        Same pattern as the CV-based stability_factor in
        DecisionEngine._calculate_confidence().
        """
        if not self.is_warmed_up:
            return 0.0
        return min(1.0, self._count / 200)

    def get_trend_slope(self) -> float:
        """Return the current trend slope (positive=rising, negative=falling)."""
        return self._trend

    def get_history(self) -> list[ForecastDataPoint]:
        """Return the full history (copy)."""
        return list(self._history)

    def get_values(self) -> list[float]:
        """Return only the values extracted from the history."""
        return [dp.value for dp in self._history]

    # =================================================================
    # StateBackend persistence (Cold Start mitigation)
    # =================================================================

    def save_state(self, metric_name: str) -> bool:
        """
        Persist the current forecaster state to the StateBackend.

        Used to restore previously learned state on Cold Start.
        Same pattern as ParameterBlacklist._save_to_storage().

        StateBackend ABC: core/state_backend.py
        - FileStateBackend: atomic JSON + tmp rename, survives restarts
        - RedisStateBackend: TTL support, shared in distributed setups

        Args:
            metric_name: Metric identifier (used in the storage key).

        Returns:
            Whether the save succeeded.
        """
        try:
            from baldur.core.state_backend import get_state_backend
            from baldur.settings.predictive_forecaster import (
                get_predictive_forecaster_settings,
            )

            settings = get_predictive_forecaster_settings()
            backend = get_state_backend()
            key = f"{self.STORAGE_KEY_PREFIX}:{metric_name}"

            state: dict[str, Any] = {
                "alpha": self._alpha,
                "beta": self._beta,
                "level": self._level,
                "trend": self._trend,
                "count": self._count,
                "warmup_samples": self._warmup_samples,
                "history": [
                    {
                        "value": dp.value,
                        "timestamp": dp.timestamp.isoformat(),
                        "has_adjustment": dp.has_adjustment,
                    }
                    for dp in self._history
                ],
            }

            backend.set(key, state, ttl_seconds=settings.state_ttl)
            logger.info(
                "holt_linear_forecaster.saved_state_points",
                metric_name=metric_name,
                count=self._count,
                time_series_level=self._level,
            )
            return True
        except Exception as e:
            logger.warning(
                "holt_linear_forecaster.save_state_failed",
                error=e,
            )
            return False

    def load_state(self, metric_name: str) -> bool:
        """
        Restore previous state from the StateBackend.

        Enables immediate prediction without Cold Start after a process
        restart. Same pattern as ParameterBlacklist._load_from_storage().

        Args:
            metric_name: Metric identifier.

        Returns:
            Whether the restore succeeded.
        """
        try:
            from baldur.core.state_backend import get_state_backend

            backend = get_state_backend()
            key = f"{self.STORAGE_KEY_PREFIX}:{metric_name}"

            state = backend.get(key)
            if state is None:
                logger.debug(
                    "holt_linear_forecaster.no_saved_state",
                    metric_name=metric_name,
                )
                return False

            self._alpha = state["alpha"]
            self._beta = state["beta"]
            self._level = state["level"]
            self._trend = state["trend"]
            self._count = state["count"]
            self._warmup_samples = state["warmup_samples"]

            self._history.clear()
            for dp_dict in state.get("history", []):
                self._history.append(
                    ForecastDataPoint(
                        value=dp_dict["value"],
                        timestamp=datetime.fromisoformat(dp_dict["timestamp"]),
                        has_adjustment=dp_dict.get("has_adjustment", False),
                    )
                )

            logger.info(
                "holt_linear_forecaster.restored_state_points",
                metric_name=metric_name,
                count=self._count,
                time_series_level=self._level,
            )
            return True
        except Exception as e:
            logger.warning(
                "holt_linear_forecaster.load_state_failed",
                error=e,
            )
            return False


# =============================================================================
# EWMAForecaster (exponentially weighted moving average — smoothing utility)
# =============================================================================


class EWMAForecaster:
    """
    Exponentially Weighted Moving Average — smoothing utility.

    HoltLinearForecaster handles trend-aware prediction;
    EWMAForecaster is used for noise removal / smoothing preprocessing.

    Uses:
    - Input smoothing for BudgetDepletionForecaster linear extrapolation
    - Noise removal before feeding ZScoreDetector
    - Not used for standalone prediction (horizontal forecasts only)

    Args:
        alpha: Smoothing factor (0 < alpha <= 1). Larger values weight
            recent data more heavily.
    """

    def __init__(self, alpha: float = 0.3):
        if not (0 < alpha <= 1):
            raise ValueError(f"alpha must be in (0, 1]: {alpha}")
        self._alpha = alpha
        self._ewma: float | None = None

    def update(self, value: float) -> float:
        """Add a new data point and return the current EWMA."""
        if self._ewma is None:
            self._ewma = value
        else:
            self._ewma = self._alpha * value + (1 - self._alpha) * self._ewma
        return self._ewma

    def get_smoothed(self) -> float | None:
        """Return the current smoothed value."""
        return self._ewma

    def reset(self) -> None:
        """Reset state."""
        self._ewma = None


# =============================================================================
# HoltWintersForecaster (triple exponential smoothing — seasonality)
# =============================================================================


class HoltWintersForecaster:
    """
    Holt-Winters Triple Exponential Smoothing.

    Detects and forecasts seasonality patterns. Adds a seasonality
    parameter (gamma) on top of HoltLinearForecaster's level + trend.

    Activates after sufficient history has accumulated (2-3 seasons;
    2-3 days for daily patterns).

    Implemented with the standard library only — no external dependencies.
    Three parameters (alpha, beta, gamma), O(season_length) state.

    Formulas (additive model)::

        level_t     = alpha * (value_t - seasonal_{t-L}) + (1 - alpha) * (level_{t-1} + trend_{t-1})
        trend_t     = beta * (level_t - level_{t-1}) + (1 - beta) * trend_{t-1}
        seasonal_t  = gamma * (value_t - level_t) + (1 - gamma) * seasonal_{t-L}
        forecast_{t+h} = level_t + h * trend_t + seasonal_{t-L+((h-1) mod L)+1}

    Args:
        alpha: Level smoothing (0.1-0.3).
        beta: Trend smoothing (0.01-0.1).
        gamma: Seasonality smoothing (0.1-0.3).
        season_length: Season length (daily pattern = 1440 at 60s intervals).
        warmup_samples: Minimum data points (at least 2 seasons).
    """

    def __init__(
        self,
        alpha: float = 0.2,
        beta: float = 0.05,
        gamma: float = 0.2,
        season_length: int = 1440,
        warmup_samples: int | None = None,
    ):
        if not (0 < alpha <= 1):
            raise ValueError(f"alpha must be in (0, 1]: {alpha}")
        if not (0 < beta <= 1):
            raise ValueError(f"beta must be in (0, 1]: {beta}")
        if not (0 < gamma <= 1):
            raise ValueError(f"gamma must be in (0, 1]: {gamma}")
        if season_length < 2:
            raise ValueError(f"season_length must be >= 2: {season_length}")

        self._alpha = alpha
        self._beta = beta
        self._gamma = gamma
        self._season_length = season_length
        self._warmup_samples = warmup_samples or (season_length * 2)

        self._level: float | None = None
        self._trend: float = 0.0
        self._seasonal: list[float] = [0.0] * season_length
        self._count: int = 0
        self._initialized: bool = False
        self._init_buffer: list[float] = []

    @property
    def is_warmed_up(self) -> bool:
        """Whether the minimum amount of data has accumulated."""
        return self._count >= self._warmup_samples

    def _initialize_components(self, values: list[float]) -> None:
        """
        Initialize level, trend, and seasonal components from the first
        season of data.

        Initialization strategy:
        - Level: mean of the first season
        - Trend: (mean of second season - mean of first season) / season_length
        - Seasonality: value at each point - mean of the first season
        """
        L = self._season_length
        first_season = values[:L]
        self._level = sum(first_season) / L

        if len(values) >= 2 * L:
            second_season = values[L : 2 * L]
            second_avg = sum(second_season) / L
            self._trend = (second_avg - self._level) / L
        else:
            self._trend = 0.0

        for i in range(L):
            self._seasonal[i] = first_season[i] - self._level

        self._initialized = True

    def update(self, value: float) -> float:
        """
        Add a new data point and return the current level.

        During the first season, values accumulate in the init buffer;
        Holt-Winters updates start after the season completes.

        Args:
            value: Observed value.

        Returns:
            Current smoothed level.
        """
        self._count += 1
        L = self._season_length

        if not self._initialized:
            self._init_buffer.append(value)
            if len(self._init_buffer) >= L:
                self._initialize_components(self._init_buffer)
            else:
                return value

        idx = (self._count - 1) % L
        prev_level = self._level or value
        prev_trend = self._trend

        self._level = self._alpha * (value - self._seasonal[idx]) + (
            1 - self._alpha
        ) * (prev_level + prev_trend)
        self._trend = (
            self._beta * (self._level - prev_level) + (1 - self._beta) * prev_trend
        )
        self._seasonal[idx] = (
            self._gamma * (value - self._level)
            + (1 - self._gamma) * self._seasonal[idx]
        )

        return self._level

    def predict(self, steps_ahead: int = 5) -> float | None:
        """
        Return the forecast N steps ahead (seasonality included).

        Args:
            steps_ahead: Number of future steps to forecast.

        Returns:
            Forecast value, or None when data is insufficient.
        """
        if self._level is None or not self.is_warmed_up:
            return None

        L = self._season_length
        seasonal_idx = (self._count + steps_ahead - 1) % L
        return self._level + steps_ahead * self._trend + self._seasonal[seasonal_idx]

    def get_confidence(self) -> float:
        """Current forecast confidence (0.0 - 1.0)."""
        if not self.is_warmed_up:
            return 0.0
        full_confidence_at = self._season_length * 3
        return min(1.0, self._count / full_confidence_at)

    def get_trend_slope(self) -> float:
        """Return the current trend slope."""
        return self._trend

    # =========================================================================
    # Phase 4: automatic seasonality detection (auto-detect season_length)
    # =========================================================================

    @staticmethod
    def detect_season_length(
        values: list[float],
        min_period: int = 2,
        max_period: int | None = None,
    ) -> int | None:
        """
        Autocorrelation-based automatic seasonality period detection.

        Computes the autocorrelation function of the time series and
        returns the length of the strongest periodic pattern.

        Implemented with the standard library only (zero-dependency
        principle).

        Algorithm:
        1. Remove the data mean (centering)
        2. Compute the autocorrelation coefficient for each lag
        3. Return the lag of the first significant peak as season_length

        Args:
            values: Time-series data (at least 2*max_period recommended).
            min_period: Minimum period to search (default 2).
            max_period: Maximum period to search (default: len(values)//3).

        Returns:
            Detected seasonality period. None = no significant seasonality.

        Example::

            values = TimeSeriesScenarioGenerator.seasonal_pattern(period=24, steps=240)
            detected = HoltWintersForecaster.detect_season_length(values)
            # detected ~= 24
        """
        n = len(values)
        if n < min_period * 4:
            return None

        if max_period is None:
            max_period = n // 3

        max_period = min(max_period, n // 2)
        if max_period < min_period:
            return None

        # Remove the mean
        mean = sum(values) / n
        centered = [v - mean for v in values]

        # Variance (denominator of lag=0 autocorrelation = 1.0)
        variance = sum(c * c for c in centered)
        if variance == 0:
            return None

        # Compute autocorrelation for each lag
        autocorrelations: list[float] = []
        for lag in range(min_period, max_period + 1):
            acf = sum(centered[i] * centered[i + lag] for i in range(n - lag))
            autocorrelations.append(acf / variance)

        if not autocorrelations:
            return None

        # Find the first significant peak.
        # Peak: a point greater than both its neighbors.
        # Significant: autocorrelation > 0.3 (noise rejection).
        best_lag = None
        best_acf = 0.3  # minimum threshold

        for i in range(1, len(autocorrelations) - 1):
            acf = autocorrelations[i]
            if (
                acf > best_acf
                and acf > autocorrelations[i - 1]
                and acf > autocorrelations[i + 1]
            ):
                best_acf = acf
                best_lag = min_period + i
                break  # select the first meaningful peak

        return best_lag
