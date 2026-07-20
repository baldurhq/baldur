"""
Anti-Flapping Settings - Pydantic v2.

Hysteresis guard settings for flapping prevention.

Replaces:
- services/coordination/anti_flapping.py:EMERGENCY_LEVEL_COOLDOWN_SECONDS
- services/coordination/anti_flapping.py:AntiFlappingGuard defaults

Environment Variables:
    BALDUR_ANTI_FLAPPING_LEVEL_COOLDOWN_SECONDS=300
    BALDUR_ANTI_FLAPPING_RECOVERY_HYSTERESIS_FACTOR=1.15
"""

import structlog
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config
from baldur.settings.field_types import SmallCount

logger = structlog.get_logger()


class AntiFlappingSettings(BaseSettings):
    """
    Anti-flapping hysteresis guard settings.

    Prevents flapping where levels change frequently causing system oscillation.

    Features:
    - Minimum wait time between Emergency Level transitions (cooldown)
    - Post-Recovery reactivation restriction (Post-Recovery Cooldown)
    - Flapping detection and auto-lock
    - Recovery Hysteresis Factor: additional stabilization time on recovery
    """

    model_config = make_settings_config("BALDUR_ANTI_FLAPPING_")

    # ==========================================================================
    # Cooldown Settings (from anti_flapping.py)
    # ==========================================================================
    level_cooldown_seconds: int = Field(
        default=300,
        ge=30,
        le=3600,
        description="Minimum wait time between level transitions (seconds)",
    )

    cooldown_after_recovery_seconds: int = Field(
        default=600,
        ge=60,
        le=7200,
        description="Post-recovery reactivation restriction time (seconds)",
    )

    # ==========================================================================
    # Stability Settings
    # ==========================================================================
    min_stable_duration_before_recovery_seconds: int = Field(
        default=600,
        ge=60,
        le=7200,
        description="Minimum stable duration before recovery (seconds)",
    )

    max_level_transitions_per_hour: SmallCount = Field(
        default=3,
        description="Max transitions per hour (flapping detection threshold)",
    )

    # ==========================================================================
    # Lockout Settings
    # ==========================================================================
    flapping_lockout_minutes: int = Field(
        default=30,
        ge=5,
        le=180,
        description="Forced lockout time on flapping detection (minutes)",
    )

    # ==========================================================================
    # Hysteresis Settings (doc 72 §5.1.1)
    # ==========================================================================
    recovery_hysteresis_factor: float = Field(
        default=1.15,
        ge=1.0,
        le=2.0,
        description=(
            "Recovery window hysteresis factor. "
            "1.15 = 15% longer time needed for recovery condition check (recommended)"
        ),
    )

    # ==========================================================================
    # AntiFlappingWindow Settings (from services/idempotency_service.py)
    # ==========================================================================
    window_seconds: int = Field(
        default=60,
        ge=10,
        le=600,
        description="AntiFlappingWindow sliding window size (seconds)",
    )

    similarity_threshold: float = Field(
        default=0.01,
        ge=0.001,
        le=0.5,
        description="Similarity judgment threshold (0.01 = within 1% = similar)",
    )

    max_similar_changes: SmallCount = Field(
        default=3,
        description="Max similar changes within window (exceeding triggers flapping)",
    )

    @field_validator("recovery_hysteresis_factor")
    @classmethod
    def validate_hysteresis_factor(cls, v: float) -> float:
        """Hysteresis factor warning."""
        if v < 1.1:
            logger.warning(
                "safe_default.low_recommend_stability",
                setting_value=v,
            )
        if v > 1.5:
            logger.warning(
                "safe_default.high_delay_recovery_too",
                setting_value=v,
            )
        return v


def get_anti_flapping_settings() -> "AntiFlappingSettings":
    from baldur.settings.root import get_config

    return get_config().services_group.anti_flapping


def reset_anti_flapping_settings() -> None:
    from baldur.settings.root import get_config

    try:
        del get_config().services_group.__dict__["anti_flapping"]
    except KeyError:
        pass
