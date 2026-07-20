"""
RuntimeFeedbackLoop Settings - Pydantic v2.

Autonomous adjustment settings for the real-time feedback loop.
Consecutive failures, rollback cooldown, post-adjustment wait time, and more
are configurable through environment variables.

Environment Variables:
    BALDUR_RUNTIME_FEEDBACK_MAX_CONSECUTIVE_FAILURES=3
    BALDUR_RUNTIME_FEEDBACK_ROLLBACK_COOLDOWN=120
    BALDUR_RUNTIME_FEEDBACK_ADJUSTMENT_WAIT=30
"""

from pydantic import Field
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config
from baldur.settings.field_types import SmallCount


class RuntimeFeedbackSettings(BaseSettings):
    """
    RuntimeFeedbackLoop settings.

    Settings for automatic rollback and pausing the feedback loop when
    autonomous adjustment fails.
    """

    model_config = make_settings_config("BALDUR_RUNTIME_FEEDBACK_")

    # ==========================================================================
    # Consecutive failure settings
    # ==========================================================================
    max_consecutive_failures: SmallCount = Field(
        default=3,
        description="Maximum consecutive failure count. Auto-pauses feedback loop when exceeded.",
    )

    # ==========================================================================
    # Rollback settings
    # ==========================================================================
    rollback_cooldown: int = Field(
        default=120,
        ge=10,
        le=3600,
        description="Stabilization wait time after rollback (seconds). Blocks further adjustments during this period.",
    )

    # ==========================================================================
    # Post-adjustment wait settings
    # ==========================================================================
    adjustment_wait: int = Field(
        default=30,
        ge=5,
        le=600,
        description="Wait time after adjustment to verify effect (seconds). For metrics collection.",
    )

    # ==========================================================================
    # Degradation detection thresholds (338: Settings Gap Phase 2)
    # ==========================================================================
    error_increase_threshold: float = Field(
        default=0.2,
        ge=0.01,
        le=1.0,
        description="Error rate increase ratio to detect degradation (20% default).",
    )
    latency_increase_threshold: float = Field(
        default=0.5,
        ge=0.05,
        le=5.0,
        description="Latency increase ratio to detect degradation (50% default).",
    )
    zero_to_error_threshold: float = Field(
        default=0.05,
        ge=0.001,
        le=0.5,
        description="Error rate threshold for zero-to-error spike detection (5% default).",
    )


def get_runtime_feedback_settings() -> "RuntimeFeedbackSettings":
    from baldur.settings.root import get_config

    return get_config().meta.runtime_feedback


def reset_runtime_feedback_settings() -> None:
    from baldur.settings.root import get_config

    try:
        del get_config().meta.__dict__["runtime_feedback"]
    except KeyError:
        pass
