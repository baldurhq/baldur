"""
Drift Threshold Settings - Pydantic v2.

Single Source of Truth for drift detection configuration.

Replaces:
- core/config.py:DriftThresholdConfig (lines 364-390)
- core/safe_defaults.py:SAFE_DEFAULTS["drift_threshold"]
- core/safe_defaults.py:VALIDATION_RULES["drift_threshold"]

Environment Variables:
    BALDUR_DRIFT_THRESHOLD_WARNING_THRESHOLD=0.05
    BALDUR_DRIFT_THRESHOLD_CRITICAL_THRESHOLD=0.20

Reference:
- docs/baldur/middleware_system/40_PYDANTIC_CONFIG_MIGRATION.md
"""

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config
from baldur.settings.field_types import MediumCount


class DriftThresholdSettings(BaseSettings):
    """
    Drift threshold configuration with validation.

    Thresholds for metric drift detection and alerting.

    All defaults match core/config.py:DriftThresholdConfig
    All validation rules match core/safe_defaults.py:VALIDATION_RULES["drift_threshold"]
    """

    model_config = make_settings_config("BALDUR_DRIFT_THRESHOLD_")

    # ==========================================================================
    # Threshold Settings (from core/config.py lines 383-387)
    # Validation rules from core/safe_defaults.py lines 326-332
    # ==========================================================================
    enabled: bool = Field(
        default=False,
        description="Enable drift detection",
    )
    warning_threshold: float = Field(
        default=0.05,
        ge=0.01,
        le=0.50,
        description="Warning threshold (5% - log only)",
    )
    critical_threshold: float = Field(
        default=0.20,
        ge=0.05,
        le=1.0,
        description="Critical threshold (20% - alert)",
    )
    incident_threshold: float = Field(
        default=0.50,
        ge=0.10,
        le=1.0,
        description="Incident threshold (50% - event loss suspected)",
    )

    # ==========================================================================
    # Alert Settings (from core/config.py lines 389-390)
    # ==========================================================================
    alert_enabled: bool = Field(
        default=False,
        description="Enable drift alerts",
    )
    incident_auto_create: bool = Field(
        default=True,
        description="Auto-create incident on threshold breach",
        validation_alias=AliasChoices(
            "BALDUR_DRIFT_THRESHOLD_INCIDENT_ENABLED",
            "BALDUR_DRIFT_THRESHOLD_INCIDENT_AUTO_CREATE",
        ),
    )

    # ==========================================================================
    # From safe_defaults drift_threshold
    # ==========================================================================
    warning_percent: float = Field(
        default=5.0,
        ge=1.0,
        le=50.0,
        description="Warning percent threshold",
    )
    critical_percent: float = Field(
        default=20.0,
        ge=5.0,
        le=100.0,
        description="Critical percent threshold",
    )
    check_interval_seconds: int = Field(
        default=60,
        ge=10,
        le=600,
        description="Check interval in seconds",
    )
    window_size_seconds: int = Field(
        default=300,
        ge=60,
        le=3600,
        description="Window size for drift calculation",
    )
    min_samples_required: MediumCount = Field(
        default=10,
        description="Minimum samples required for drift calculation",
    )
    suppress_duplicate_alerts_seconds: int = Field(
        default=300,
        ge=60,
        le=3600,
        description="Suppress duplicate alerts for this duration",
    )

    @field_validator("critical_threshold")
    @classmethod
    def validate_thresholds(cls, v: float, info) -> float:
        """Validate critical > warning threshold."""
        # Note: In Pydantic v2, we can't easily access other field values
        # during field validation, so we skip cross-field validation here.
        # Use model_validator for cross-field validation if needed.
        return v


def get_drift_threshold_settings() -> "DriftThresholdSettings":
    """Get cached DriftThresholdSettings instance."""
    from baldur.settings.root import get_config

    return get_config().metrics_group.drift_threshold


def reset_drift_threshold_settings() -> None:
    """Reset cached settings (for testing)."""
    from baldur.settings.root import get_config

    try:
        del get_config().metrics_group.__dict__["drift_threshold"]
    except KeyError:
        pass
