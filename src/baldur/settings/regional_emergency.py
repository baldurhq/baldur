"""
Regional Emergency Settings - Pydantic v2.

Settings for per-region independent Emergency state management and cascading
failure detection.

Source:
- services/regional_emergency/cascade_detector.py
- services/regional_emergency/tracker.py
- services/regional_emergency/escalation_audit.py

Environment Variables:
    BALDUR_REGIONAL_EMERGENCY_ESCALATION_THRESHOLD=2
    BALDUR_REGIONAL_EMERGENCY_CASCADE_WINDOW_MINUTES=30
    BALDUR_REGIONAL_EMERGENCY_EXPIRY_HOURS=8
    BALDUR_REGIONAL_EMERGENCY_CACHE_TTL_SECONDS=30.0
    BALDUR_REGIONAL_EMERGENCY_MAX_BUFFER_SIZE=1000
"""

import structlog
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config

logger = structlog.get_logger()


class RegionalEmergencySettings(BaseSettings):
    """
    Namespace Emergency settings.

    Defines multi-region cascading failure detection, state tracking, and audit
    trail settings.
    """

    model_config = make_settings_config("BALDUR_REGIONAL_EMERGENCY_")

    # ==========================================================================
    # Cascade Detection (from cascade_detector.py lines 44-47)
    # ==========================================================================
    escalation_threshold: int = Field(
        default=2,
        ge=1,
        le=10,
        description="GLOBAL escalation threshold: triggers cascade when N or more regions are STRICT",
    )
    cascade_window_minutes: int = Field(
        default=30,
        ge=5,
        le=120,
        description="Cascade detection time window (minutes)",
    )

    # ==========================================================================
    # Tracker Settings (from tracker.py lines 48-51)
    # ==========================================================================
    expiry_hours: int = Field(
        default=8,
        ge=1,
        le=72,
        description="Default expiry time for Emergency state (hours)",
    )
    cache_ttl_seconds: float = Field(
        default=30.0,
        ge=1.0,
        le=300.0,
        description="Local cache TTL (seconds)",
    )

    # ==========================================================================
    # Audit Trail (from escalation_audit.py line 203)
    # ==========================================================================
    max_buffer_size: int = Field(
        default=1000,
        ge=100,
        le=100000,
        description="Maximum memory buffer size (number of audit events)",
    )

    @field_validator("escalation_threshold")
    @classmethod
    def validate_escalation_threshold(cls, v: int) -> int:
        """Warn when escalation_threshold is too small."""
        if v < 2:
            logger.warning(
                "regional_emergency_settings.low_consider_using_avoid",
                setting_value=v,
            )
        return v


def get_regional_emergency_settings() -> "RegionalEmergencySettings":
    from baldur.settings.root import get_config

    return get_config().multi_region.regional_emergency


def reset_regional_emergency_settings() -> None:
    from baldur.settings.root import get_config

    try:
        del get_config().multi_region.__dict__["regional_emergency"]
    except KeyError:
        pass
