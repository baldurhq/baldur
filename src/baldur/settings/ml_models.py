"""
ML model strategy settings.

Provides configuration for ML-based strategy implementations:
- IsolationForest (anomaly detection)
- ARIMA (time series forecasting)
- GradientBoost (classification)
- BayesianOptimizer (parameter optimization)

Each model has an independent enabled flag and validators.
Env vars use env_nested_delimiter="__" (settings/base.py:16).

Env var examples:
    BALDUR_ML_MODELS_ENABLED=true
    BALDUR_ML_MODELS_ISOLATION_FOREST__CONTAMINATION=0.05
    BALDUR_ML_MODELS_ARIMA__AUTO_ORDER=false
    BALDUR_ML_MODELS_GRADIENT_BOOST__PREFER_XGBOOST=false
    BALDUR_ML_MODELS_BAYESIAN__MAX_OBSERVATIONS=300
"""

from __future__ import annotations

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config

__all__ = [
    "ARIMAConfig",
    "BayesianOptimizerConfig",
    "GradientBoostConfig",
    "IsolationForestConfig",
    "MLModelsSettings",
    "get_ml_models_settings",
    "reset_ml_models_settings",
]


class IsolationForestConfig(BaseModel):
    """IsolationForest detector configuration."""

    enabled: bool = Field(
        default=False,
        description="Enable IsolationForest when global ML feature is on.",
    )
    contamination: float = Field(default=0.05, ge=0.01, le=0.5)
    n_estimators: int = Field(default=100, ge=10, le=1000)
    min_data_points: int = Field(default=200, ge=50, le=10000)
    max_samples: int = Field(default=256, ge=64, le=2048)
    max_buffer_size: int = Field(
        default=2560,
        ge=500,
        le=50000,
        description="Data buffer max size (deque maxlen). Default = max_samples × 10.",
    )
    refit_threshold: int = Field(default=500, ge=100, le=10000)


class ARIMAConfig(BaseModel):
    """ARIMA forecaster configuration."""

    enabled: bool = Field(default=False)
    order: tuple[int, int, int] = Field(default=(2, 1, 2))
    auto_order: bool = Field(default=True)
    min_data_points: int = Field(default=50, ge=20, le=1000)
    max_history: int = Field(default=5000, ge=500, le=50000)
    refit_interval: int = Field(default=100, ge=10, le=1000)


class GradientBoostConfig(BaseModel):
    """Gradient Boost classifier configuration."""

    enabled: bool = Field(default=False)
    n_estimators: int = Field(default=100, ge=10, le=1000)
    max_depth: int = Field(default=6, ge=2, le=15)
    learning_rate: float = Field(default=0.1, ge=0.001, le=1.0)
    min_data_points: int = Field(default=200, ge=50, le=10000)
    prefer_xgboost: bool = Field(default=True)
    retrain_threshold: int = Field(default=100, ge=10, le=1000)


class BayesianOptimizerConfig(BaseModel):
    """Bayesian optimizer configuration."""

    enabled: bool = Field(default=False)
    kernel: str = Field(default="matern")
    acquisition: str = Field(default="expected_improvement")
    exploration_weight: float = Field(default=0.1, ge=0.0, le=1.0)
    min_data_points: int = Field(default=20, ge=5, le=200)
    max_observations: int = Field(default=500, ge=50, le=5000)


class MLModelsSettings(BaseSettings):
    """ML model strategy settings.

    Nested BaseModel pattern: each model has its own Config with
    independent enabled flag and validators. Env vars use
    env_nested_delimiter="__" (settings/base.py:16).

    Env var examples:
        BALDUR_ML_MODELS_ENABLED=true
        BALDUR_ML_MODELS_ISOLATION_FOREST__CONTAMINATION=0.05
        BALDUR_ML_MODELS_ARIMA__AUTO_ORDER=false
        BALDUR_ML_MODELS_GRADIENT_BOOST__PREFER_XGBOOST=false
        BALDUR_ML_MODELS_BAYESIAN__MAX_OBSERVATIONS=300
    """

    model_config = make_settings_config("BALDUR_ML_MODELS_")

    enabled: bool = Field(
        default=False,
        description="Global ML feature toggle. When False, all ML models disabled.",
    )
    isolation_forest: IsolationForestConfig = Field(
        default_factory=IsolationForestConfig,
    )
    arima: ARIMAConfig = Field(default_factory=ARIMAConfig)
    gradient_boost: GradientBoostConfig = Field(
        default_factory=GradientBoostConfig,
    )
    bayesian: BayesianOptimizerConfig = Field(
        default_factory=BayesianOptimizerConfig,
    )


# ── Singleton ──


def get_ml_models_settings() -> MLModelsSettings:
    """Return cached MLModelsSettings via RootConfig."""
    from baldur.settings.root import get_config

    return get_config().services_group.ml_models


def reset_ml_models_settings() -> None:
    """Reset cached MLModelsSettings (for testing)."""
    from baldur.settings.root import get_config

    try:
        del get_config().services_group.__dict__["ml_models"]
    except KeyError:
        pass
