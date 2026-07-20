"""
Corruption Shield Settings - Pydantic v2.

Corruption Shield settings for data integrity protection.

Replaces:
- services/corruption_shield/config.py:CorruptionShieldConfig (hardcoded
  defaults)

Environment Variables:
    BALDUR_CORRUPTION_SHIELD_Z_SCORE_THRESHOLD=3.0
    BALDUR_CORRUPTION_SHIELD_IQR_MULTIPLIER=1.5
    BALDUR_CORRUPTION_SHIELD_MIN_SAMPLES_FOR_ANOMALY=10
    BALDUR_CORRUPTION_SHIELD_MAX_AMOUNT=100000000
"""

import structlog
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config
from baldur.settings.validators import warn_below

logger = structlog.get_logger()


class CorruptionShieldSettings(BaseSettings):
    """
    Corruption Shield settings.

    Three-layer data integrity protection:
    - L1: Schema validation (required fields, types)
    - L2: Business rules (amount range, allowed statuses)
    - L3: Anomaly detection (Z-score, IQR)
    """

    model_config = make_settings_config("BALDUR_CORRUPTION_SHIELD_")

    # ==========================================================================
    # Global Kill-Switch
    # ==========================================================================
    enabled: bool = Field(
        default=False,
        description="Global kill-switch for corruption shield. "
        "When False, all layers are bypassed regardless of individual l*_enabled flags.",
    )

    # ==========================================================================
    # Layer Enable/Disable (from corruption_shield/config.py)
    # ==========================================================================
    l1_enabled: bool = Field(
        default=False,
        description="Enable L1 schema validation",
    )

    l2_enabled: bool = Field(
        default=False,
        description="Enable L2 business rule validation",
    )

    l3_enabled: bool = Field(
        default=False,
        description="Enable L3 anomaly detection",
    )

    # ==========================================================================
    # L1: Schema Validation (from corruption_shield/config.py)
    # ==========================================================================
    required_fields: list[str] = Field(
        default_factory=lambda: ["amount", "order_id"],
        description="List of required fields",
    )

    max_string_length: int = Field(
        default=1000,
        ge=100,
        le=10000,
        description="Maximum string length",
    )

    # ==========================================================================
    # L2: Business Rules (from corruption_shield/config.py#L25-26)
    # ==========================================================================
    min_amount: int = Field(
        default=100,
        ge=0,
        le=10000,
        description="Minimum amount (KRW)",
    )

    max_amount: int = Field(
        default=100_000_000,
        ge=100000,
        le=1_000_000_000,
        description="Maximum amount (KRW, default 100M)",
    )

    allowed_statuses: list[str] = Field(
        default_factory=lambda: ["DONE", "CANCELED", "PENDING"],
        description="List of allowed status values",
    )

    # ==========================================================================
    # L3: Anomaly Detection (from corruption_shield/config.py#L29-31)
    # ==========================================================================
    z_score_threshold: float = Field(
        default=3.0,
        ge=1.0,
        le=10.0,
        description="Z-score threshold (standard deviations)",
    )

    iqr_multiplier: float = Field(
        default=1.5,
        ge=1.0,
        le=5.0,
        description="IQR outlier multiplier",
    )

    min_samples_for_anomaly: int = Field(
        default=10,
        ge=5,
        le=1000,
        description="Minimum sample count for anomaly detection",
    )

    # ==========================================================================
    # Logging Settings (from corruption_shield/config.py#L34-35)
    # ==========================================================================
    log_violations: bool = Field(
        default=True,
        description="Enable violation logging",
    )

    log_to_security_incident: bool = Field(
        default=True,
        description="Log as security incident",
    )

    # ==========================================================================
    # Emergency Escalation Settings
    # ==========================================================================
    emergency_escalation_enabled: bool = Field(
        default=False,
        description="Enable emergency escalation on critical violations",
    )

    emergency_level2_threshold: int = Field(
        default=3,
        ge=1,
        le=20,
        description="Number of critical violations to trigger LEVEL_2 (below: LEVEL_1)",
    )

    emergency_window_seconds: int = Field(
        default=300,
        ge=60,
        le=3600,
        description="Time window for critical violation accumulation",
    )

    @field_validator("z_score_threshold")
    @classmethod
    def _warn_z_score(cls, v: float) -> float:
        """Warn when the Z-score is too low."""
        return warn_below(2.0, "corruption_shield.z_score_too_sensitive")(v)

    @field_validator("min_amount", "max_amount")
    @classmethod
    def validate_amount_range(cls, v: int, info) -> int:
        """Log the amount range."""
        if info.field_name == "max_amount" and v > 500_000_000:
            logger.info(
                "corruption_shield.large_transaction_amount_set",
                setting_value=v,
            )
        return v


def get_corruption_shield_settings() -> "CorruptionShieldSettings":
    from baldur.settings.root import get_config

    return get_config().security_group.corruption_shield


def reset_corruption_shield_settings() -> None:
    from baldur.settings.root import get_config

    try:
        del get_config().security_group.__dict__["corruption_shield"]
    except KeyError:
        pass
