"""ML Settings Recommendation configuration.

Environment Variables:
    BALDUR_RECOMMENDATION_ENABLED=false
    BALDUR_RECOMMENDATION_MODE=rule_based
    BALDUR_RECOMMENDATION_AUTO_APPLY=false
    BALDUR_RECOMMENDATION_MIN_CONFIDENCE=0.7
    BALDUR_RECOMMENDATION_SCHEDULE_SECONDS=3600
    BALDUR_RECOMMENDATION_CANARY_STAGES='[{"percentage":100,"duration_minutes":0}]'
"""

from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings

from baldur.services.settings_recommendation.models import CanaryStageConfig
from baldur.settings.base import make_settings_config
from baldur.settings.field_types import (
    LargeCount,
    MediumDuration,
    Probability,
    SmallCount,
)

__all__ = [
    "SettingsRecommendationSettings",
    "get_settings_recommendation_settings",
    "reset_settings_recommendation_settings",
]


class SettingsRecommendationSettings(BaseSettings):
    """ML Settings Recommendation configuration."""

    model_config = make_settings_config("BALDUR_RECOMMENDATION_")

    # Feature toggle
    enabled: bool = False
    mode: Literal["rule_based", "ml_assisted", "ml_primary"] = "rule_based"
    auto_apply: bool = False

    # Thresholds
    min_confidence: Probability = 0.7
    max_changes_per_cycle: SmallCount = 5

    # Scheduling
    schedule_seconds: int = Field(default=3600, ge=60, le=86400)
    cooldown_seconds: int = Field(default=7200, ge=300, le=86400)

    # Validation gates
    shadow_required: bool = True
    canary_required: bool = True

    # ML settings
    ml_min_data_points: int = Field(default=100, ge=10, le=10000)
    ml_objective_metrics: list[str] = Field(
        default=["error_rate", "p99_latency_ms"],
        description="ML optimization target metrics for BayesianOptimizer.suggest_batch()",
    )
    fallback_to_rules: bool = True

    # Profile presets
    workload_profile: str | None = None  # Auto-detect if None

    # Canary stages
    canary_stages: list[CanaryStageConfig] = Field(
        default=[
            CanaryStageConfig(percentage=10, duration_minutes=30),
            CanaryStageConfig(percentage=50, duration_minutes=60),
            CanaryStageConfig(percentage=100, duration_minutes=0),
        ],
        description="Canary rollout stages (percentage + duration)",
    )

    # ML history grouping
    history_grouping_window_seconds: int = Field(
        default=30,
        ge=5,
        le=300,
        description="Window to group concurrent adjustment records into single ML observation",
    )

    # Prediction
    prediction_steps: int = Field(default=3, ge=1, le=10)

    # Storage & State
    max_plans: LargeCount = 200
    pipeline_timeout_seconds: MediumDuration = 30.0
    state_save_interval_seconds: int = Field(default=600, ge=60, le=3600)

    @model_validator(mode="after")
    def validate_canary_stages(self) -> Self:
        """Canary stage integrity validation (fail-fast)."""
        if not self.canary_stages:
            raise ValueError("canary_stages must have at least one stage")
        percentages = [s.percentage for s in self.canary_stages]
        if percentages != sorted(percentages):
            raise ValueError("canary_stages percentages must be in ascending order")
        if percentages[-1] != 100:
            raise ValueError("last canary stage must have percentage=100")
        for s in self.canary_stages:
            if s.duration_minutes < 0:
                raise ValueError(
                    f"canary stage duration must be >= 0, got {s.duration_minutes}"
                )
        return self


def get_settings_recommendation_settings() -> SettingsRecommendationSettings:
    from baldur.settings.root import get_config

    return get_config().services_group.settings_recommendation


def reset_settings_recommendation_settings() -> None:
    from baldur.settings.root import get_config

    try:
        del get_config().services_group.__dict__["settings_recommendation"]
    except KeyError:
        pass
