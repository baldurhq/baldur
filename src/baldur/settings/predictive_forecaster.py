"""
Predictive Anomaly Forecaster Settings - Pydantic v2.

Settings for the time-series-forecasting-based anomaly detection engine.
Injects all prediction-system parameters via env vars: HoltLinearForecaster,
ZScoreDetector, IQRDetector, SpikeClassifier, StateBackend persistence, etc.

Env var prefix: BALDUR_PREDICTIVE_FORECASTER_

Usage:
    from baldur.settings.predictive_forecaster import (
        get_predictive_forecaster_settings,
    )

    settings = get_predictive_forecaster_settings()
    alpha = settings.ewma_alpha
    beta = settings.holt_beta
"""

from __future__ import annotations

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config


class PredictiveForecasterSettings(BaseSettings):
    """
    Predictive Anomaly Forecaster settings.

    Manages parameters for the HoltLinearForecaster (double exponential smoothing)
    based time-series forecasting engine, ZScoreDetector/IQRDetector anomaly
    detection, the SpikeClassifier spike classifier, StateBackend-based Cold Start
    prevention, etc.

    Same Pydantic v2 BaseSettings + env_prefix pattern as
    DomainSensitivitySettings (settings/domain_sensitivity.py).
    """

    model_config = make_settings_config("BALDUR_PREDICTIVE_FORECASTER_")

    # ── Feature toggle ──

    enabled: bool = Field(
        default=False,
        description="Enable predictive forecaster",
    )

    # ── HoltLinearForecaster (double exponential smoothing forecaster) ──

    ewma_alpha: float = Field(
        default=0.3,
        ge=0.01,
        le=1.0,
        description="Level smoothing coefficient (alpha). Also shared with EWMAForecaster smoothing.",
    )
    holt_beta: float = Field(
        default=0.1,
        ge=0.001,
        le=1.0,
        description=(
            "Holt Linear trend smoothing coefficient (beta). "
            "Lower values are conservative to trend changes (0.01-0.05: long-term), "
            "higher values are sensitive to trend changes (0.1-0.3: short-term)."
        ),
    )

    # ── Anomaly detection ──

    zscore_threshold: float = Field(
        default=3.0,
        ge=1.0,
        le=5.0,
        description="Z-Score anomaly detection threshold (3.0 = 99.7% confidence interval).",
    )
    zscore_window: int = Field(
        default=100,
        ge=10,
        le=10000,
        description="Z-Score moving window size.",
    )
    iqr_multiplier: float = Field(
        default=1.5,
        ge=1.0,
        le=5.0,
        description="IQR anomaly detection multiplier (1.5 = normal, 3.0 = extreme).",
    )

    # ── History / Cold Start ──

    max_history: int = Field(
        default=10000,
        ge=100,
        le=10000,
        description="Prediction history ring buffer size.",
    )
    warmup_samples: int = Field(
        default=30,
        ge=5,
        le=1000,
        description=(
            "Cold Start protection: returns confidence 0 when fewer than "
            "this many data points are available, suppressing prediction-based actions."
        ),
    )

    # ── Prediction ──

    prediction_steps: int = Field(
        default=5,
        ge=1,
        le=30,
        description="Default prediction steps (at 60s intervals, 5 = 5 minutes ahead).",
    )

    # ── Proactive action ──

    min_confidence_for_action: float = Field(
        default=0.7,
        ge=0.0,
        le=1.0,
        description="Minimum prediction confidence required to execute proactive actions.",
    )
    dry_run: bool = Field(
        default=True,
        description="Default DRY_RUN for initial rollout. Set to False after prediction accuracy is verified.",
    )

    # ── Sensitivity ──

    sensitivity_multiplier: float = Field(
        default=1.0,
        ge=0.1,
        le=100.0,
        description=(
            "Domain-specific sensitivity multiplier. "
            "Same 0.1-100.0 range as DomainSensitivitySettings. "
            "Use 10.0+ for payment domains, 0.5 for low importance, etc. "
            "Applied inversely to SpikeClassifier thresholds "
            "(higher sensitivity = lower threshold = more sensitive detection)."
        ),
    )

    # ── SpikeClassifier history buffer ──

    spike_history_size: int = Field(
        default=200,
        ge=50,
        le=5000,
        description=(
            "Multi-signal history ring buffer size for SpikeClassifier. "
            "SpikeClassifier uses only the last 5-10 entries, so the default 200 provides 20x headroom."
        ),
    )

    # ── SpikeClassifier (spike type classifier) ──

    spike_error_rate_threshold: float = Field(
        default=0.05,
        ge=0.001,
        le=1.0,
        description=(
            "SpikeClassifier: error rate delta exceeding this value is classified "
            "as ANOMALOUS_SPIKE (base value before sensitivity_multiplier is applied)."
        ),
    )
    spike_acceleration_threshold: float = Field(
        default=2.0,
        ge=0.1,
        le=100.0,
        description="SpikeClassifier: RPS acceleration threshold.",
    )

    # ── StateBackend persistence ──

    state_ttl: int = Field(
        default=259200,
        ge=3600,
        le=604800,
        description="StateBackend state TTL (seconds). Default 72 hours (259,200s).",
    )

    @model_validator(mode="after")
    def validate_confidence_range(self) -> PredictiveForecasterSettings:
        """Validate that warmup_samples is sufficiently larger than prediction_steps."""
        if self.warmup_samples < self.prediction_steps:
            raise ValueError(
                f"warmup_samples({self.warmup_samples}) must be "
                f"greater than or equal to prediction_steps({self.prediction_steps})."
            )
        return self


# ── Singleton ──


def get_predictive_forecaster_settings() -> PredictiveForecasterSettings:
    from baldur.settings.root import get_config

    return get_config().services_group.predictive_forecaster


def reset_predictive_forecaster_settings() -> None:
    from baldur.settings.root import get_config

    try:
        del get_config().services_group.__dict__["predictive_forecaster"]
    except KeyError:
        pass
