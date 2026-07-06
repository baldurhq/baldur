"""
Canary Governance Settings.

Centralized governance settings for canary rollouts.

Environment Variables:
    BALDUR_CANARY_GOVERNANCE_START_EMERGENCY_MIN_LEVEL=2
    BALDUR_CANARY_GOVERNANCE_ROLLBACK_EMERGENCY_MIN_LEVEL=2
    BALDUR_CANARY_GOVERNANCE_PROMOTE_EMERGENCY_MIN_LEVEL=2
    BALDUR_CANARY_GOVERNANCE_RESUME_EMERGENCY_MIN_LEVEL=2
    BALDUR_CANARY_GOVERNANCE_BYPASS_MIN_REASON_LENGTH=10
"""

from pydantic import Field
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config


class CanaryGovernanceSettings(BaseSettings):
    """
    Canary Governance settings.

    Governance checks and per-method emergency level configuration.
    """

    model_config = make_settings_config("BALDUR_CANARY_GOVERNANCE_")

    # ==========================================================================
    # Per-method emergency_min_level settings (D-1)
    # ==========================================================================
    start_emergency_min_level: int = Field(
        default=2,
        ge=2,
        le=3,
        description="Emergency min level for start_rollout governance",
    )
    rollback_emergency_min_level: int = Field(
        default=2,
        ge=2,
        le=3,
        description="Emergency min level for rollback governance",
    )
    promote_emergency_min_level: int = Field(
        default=2,
        ge=2,
        le=3,
        description="Emergency min level for promote/auto_promote governance",
    )
    resume_emergency_min_level: int = Field(
        default=2,
        ge=2,
        le=3,
        description="Emergency min level for resume governance (emergency-only re-check)",
    )

    # ==========================================================================
    # Bypass validation settings
    # ==========================================================================
    bypass_min_reason_length: int = Field(
        default=10,
        ge=5,
        le=500,
        description="Minimum length for governance bypass reason",
    )


# =============================================================================
# Singleton Pattern
# =============================================================================


def get_canary_governance_settings() -> "CanaryGovernanceSettings":
    """Return cached CanaryGovernanceSettings singleton."""
    from baldur.settings.root import get_config

    return get_config().services_group.canary_governance


def reset_canary_governance_settings() -> None:
    """Reset singleton (for testing)."""
    from baldur.settings.root import get_config

    try:
        del get_config().services_group.__dict__["canary_governance"]
    except KeyError:
        pass
