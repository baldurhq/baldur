"""
Governance Settings - Pydantic v2.

Single Source of Truth for governance configuration.

Replaces:
- core/config.py:GovernanceConfig (lines 322-361)
- core/safe_defaults.py:SAFE_DEFAULTS["governance"]
- core/safe_defaults.py:VALIDATION_RULES["governance"]

Environment Variables:
    BALDUR_GOVERNANCE_THRESHOLD_OPERATOR=0.15
    BALDUR_GOVERNANCE_EMERGENCY_EXPIRY_HOURS=8

    # Celery task retry settings
    BALDUR_GOVERNANCE_EXPIRY_CHECK_MAX_RETRIES=3
    BALDUR_GOVERNANCE_EXPIRY_CHECK_RETRY_DELAY=60
"""

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config
from baldur.settings.field_types import (
    StrictProbability,
)


class GovernanceSettings(BaseSettings):
    """
    Governance configuration with validation.

    RBAC thresholds, emergency mode auto-recovery, and notification settings.

    All defaults match core/config.py:GovernanceConfig
    All validation rules match core/safe_defaults.py:VALIDATION_RULES["governance"]
    """

    model_config = make_settings_config("BALDUR_GOVERNANCE_")

    # ==========================================================================
    # Risk-Based Access Control (from core/config.py lines 332-334)
    # Validation rules from core/safe_defaults.py lines 319-325
    # ==========================================================================
    threshold_operator: StrictProbability = Field(
        default=0.15,
        description="Operator approval threshold (15%)",
    )
    threshold_admin: StrictProbability = Field(
        default=0.30,
        description="Admin approval threshold (30%)",
    )

    # ==========================================================================
    # Emergency Escalation (Break Glass) (from core/config.py lines 340-343)
    # ==========================================================================
    emergency_expiry_hours: int = Field(
        default=8,
        ge=1,
        le=48,
        description="Hours until automatic emergency mode expiry",
    )
    emergency_warning_hours: int = Field(
        default=4,
        ge=1,
        le=24,
        description="Hours when warning starts",
    )
    emergency_final_warning_hours: int = Field(
        default=6,
        ge=1,
        le=24,
        description="Hours for final warning",
    )

    emergency_min_level: int = Field(
        default=2,
        ge=1,
        le=3,
        description="Minimum emergency level to bypass governance checks (1=LEVEL_1, 2=LEVEL_2, 3=LEVEL_3)",
    )

    # ==========================================================================
    # Operating Mode (from core/config.py lines 349)
    # ==========================================================================
    default_mode: str = Field(
        default="NORMAL",
        description="Default operating mode (NORMAL or STRICT)",
    )

    # ==========================================================================
    # Notification Settings (from core/config.py lines 355-360)
    # ==========================================================================
    notify_on_emergency: bool = Field(
        default=True,
        description="Send notification on emergency activation",
    )
    notify_channels: list[str] = Field(
        default_factory=lambda: ["slack"],
        description="Notification channels for governance events",
    )
    emergency_slack_channel: str = Field(
        default="#emergency-alerts",
        description="Slack channel for emergency alerts",
    )

    # ==========================================================================
    # Audit Settings (from safe_defaults.py governance)
    # ==========================================================================
    approval_timeout_hours: int = Field(
        default=24,
        ge=1,
        le=168,
        description="Approval wait timeout hours",
    )
    require_reason_for_changes: bool = Field(
        default=True,
        description="Require reason for configuration changes",
    )

    # ==========================================================================
    # Governance Check Cache TTL (from governance_checks.py line 296)
    # ==========================================================================
    cache_ttl: float = Field(
        default=30.0,
        ge=1.0,
        le=300.0,
        description="Governance check cache TTL (seconds). Used for system state caching.",
    )

    # ==========================================================================
    # Break Glass (emergency escape hatch)
    # ==========================================================================
    break_glass_enabled: bool = Field(
        default=False,
        description="Bypass all governance checks in emergencies (PIR required). Env var: BALDUR_GOVERNANCE_BREAK_GLASS_ENABLED=true",
    )

    break_glass_audit_required: bool = Field(
        default=True,
        description="Require audit logging when Break Glass is used",
    )

    # ==========================================================================
    # Celery task retry settings (check_emergency_mode_expiry_task)
    # ==========================================================================
    expiry_check_max_retries: int = Field(
        default=3,
        ge=0,
        le=10,
        description="Maximum retry count for emergency mode expiry check task",
    )
    expiry_check_retry_delay: int = Field(
        default=60,
        ge=10,
        le=600,
        description="Retry delay for emergency mode expiry check task (seconds)",
    )

    @field_validator("default_mode")
    @classmethod
    def validate_mode(cls, v: str) -> str:
        """Validate operating mode."""
        v_upper = v.upper()
        if v_upper not in {"NORMAL", "STRICT"}:
            raise ValueError("default_mode must be 'NORMAL' or 'STRICT'")
        return v_upper


# =============================================================================
# Singleton Pattern (cached settings)
# =============================================================================


def get_governance_settings() -> "GovernanceSettings":
    from baldur.settings.root import get_config

    return get_config().services_group.governance


def reset_governance_settings() -> None:
    from baldur.settings.root import get_config

    try:
        del get_config().services_group.__dict__["governance"]
    except KeyError:
        pass
