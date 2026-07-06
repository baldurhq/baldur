"""
Learning Settings - Pydantic v2.

Learning service suggestion thresholds, anomaly detection parameters,
blacklist TTL, and ThrottleSLA auto-tuning rules externalized as env vars.

Source:
- services/learning/service.py (suggestion_threshold, pattern_min, anomaly)
- services/auto_tuning/adjustment_recorder.py (MAX_RECORDS)
- services/auto_tuning/throttle_sla_rules.py (SLA adjustment rules)

Environment Variables:
    BALDUR_LEARNING_SUGGESTION_THRESHOLD=0.8
    BALDUR_LEARNING_PATTERN_MIN_OCCURRENCES=3
    BALDUR_LEARNING_ANOMALY_MULTIPLIER=2.0
    BALDUR_LEARNING_ANOMALY_WINDOW_SIZE=100
    BALDUR_LEARNING_BLACKLIST_DEFAULT_TTL_HOURS=168
    BALDUR_LEARNING_MAX_ADJUSTMENT_RECORDS=1000
    BALDUR_LEARNING_SLA_WARNING_UP__TRIGGER_RATIO=0.9
    BALDUR_LEARNING_SLA_WARNING_UP__ADJUST_MULTIPLIER=1.15
    BALDUR_LEARNING_SLA_WARNING_UP__LIMIT_BOUND=2000
    BALDUR_LEARNING_SLA_WARNING_DOWN__TRIGGER_RATIO=0.5
    BALDUR_LEARNING_SLA_CRITICAL_UP__TRIGGER_RATIO=0.85
"""

from __future__ import annotations

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config
from baldur.settings.field_types import MediumCount, Probability


class ThrottleSLARule(BaseModel):
    """Individual SLA adjustment rule."""

    trigger_ratio: Probability = Field(
        description="Metric-to-threshold ratio that triggers this rule.",
    )
    adjust_multiplier: float = Field(
        gt=0.0,
        description="Multiplier applied to current threshold.",
    )
    limit_bound: float = Field(
        gt=0.0,
        description="Cap (upward) or floor (downward) for adjusted value.",
    )
    min_confidence: Probability = Field(
        default=0.6,
        description="Minimum confidence required to apply this rule.",
    )


class LearningSettings(BaseSettings):
    """
    Learning / Auto-Tuning settings.

    Defines suggestion thresholds, anomaly detection parameters,
    blacklist TTL, and ThrottleSLA auto-tuning rules.
    """

    model_config = make_settings_config("BALDUR_LEARNING_")

    # =========================================================================
    # LearningService parameters (services/learning/service.py)
    # =========================================================================
    suggestion_threshold: Probability = Field(
        default=0.8,
        description="Minimum confidence to generate suggestions.",
    )
    pattern_min_occurrences: MediumCount = Field(
        default=3,
        description="Minimum pattern occurrence count for suggestion generation.",
    )
    anomaly_multiplier: float = Field(
        default=2.0,
        gt=1.0,
        le=10.0,
        description="Anomaly detection multiplier (value > avg * multiplier).",
    )
    anomaly_window_size: int = Field(
        default=100,
        ge=10,
        le=10000,
        description="Anomaly detection sliding window size.",
    )
    blacklist_default_ttl_hours: int | None = Field(
        default=168,
        ge=1,
        description="Default blacklist TTL in hours. None for indefinite.",
    )

    # =========================================================================
    # AdjustmentRecorder (services/auto_tuning/adjustment_recorder.py)
    # =========================================================================
    max_adjustment_records: int = Field(
        default=1000,
        ge=100,
        le=100000,
        description="Maximum adjustment records to keep.",
    )

    # =========================================================================
    # ThrottleSLA rules (services/auto_tuning/throttle_sla_rules.py)
    # =========================================================================
    sla_warning_up: ThrottleSLARule = Field(
        default_factory=lambda: ThrottleSLARule(
            trigger_ratio=0.9,
            adjust_multiplier=1.15,
            limit_bound=2000,
            min_confidence=0.6,
        ),
        description="SLA warning upward adjustment rule.",
    )
    sla_warning_down: ThrottleSLARule = Field(
        default_factory=lambda: ThrottleSLARule(
            trigger_ratio=0.5,
            adjust_multiplier=0.85,
            limit_bound=50,
            min_confidence=0.6,
        ),
        description="SLA warning downward adjustment rule.",
    )
    sla_critical_up: ThrottleSLARule = Field(
        default_factory=lambda: ThrottleSLARule(
            trigger_ratio=0.85,
            adjust_multiplier=1.15,
            limit_bound=5000,
            min_confidence=0.7,
        ),
        description="SLA critical upward adjustment rule.",
    )


def get_learning_settings() -> LearningSettings:
    """Return cached LearningSettings via RootConfig."""
    from baldur.settings.root import get_config

    return get_config().services_group.learning


def reset_learning_settings() -> None:
    """Reset cached LearningSettings (for testing)."""
    from baldur.settings.root import get_config

    try:
        del get_config().services_group.__dict__["learning"]
    except KeyError:
        pass
