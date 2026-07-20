"""
Admission Control Settings - Pydantic v2.

Admission control settings for inbound HTTP requests.
Manages per-tier bulkhead concurrency limits and enablement.

Environment Variables:
    BALDUR_ADMISSION_CONTROL_ENABLED=true
    BALDUR_ADMISSION_CONTROL_TIER_CRITICAL_MAX_CONCURRENT=100
    BALDUR_ADMISSION_CONTROL_TIER_STANDARD_MAX_CONCURRENT=50
    BALDUR_ADMISSION_CONTROL_TIER_NON_ESSENTIAL_MAX_CONCURRENT=20
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config
from baldur.settings.field_types import LargeCount


class AdmissionControlSettings(BaseSettings):
    """Inbound HTTP admission control settings."""

    model_config = make_settings_config("BALDUR_ADMISSION_CONTROL_")

    enabled: bool = Field(
        default=True,
        description="Enable/disable Admission Control",
    )

    # =========================================================================
    # Per-tier bulkhead maximum concurrent executions
    # =========================================================================
    tier_critical_max_concurrent: LargeCount = Field(
        default=100,
        description="Maximum concurrent executions for critical tier bulkhead",
    )

    tier_standard_max_concurrent: int = Field(
        default=50,
        ge=1,
        le=500,
        description="Maximum concurrent executions for standard tier bulkhead",
    )

    tier_non_essential_max_concurrent: int = Field(
        default=20,
        ge=1,
        le=200,
        description="Maximum concurrent executions for non_essential tier bulkhead",
    )

    # =========================================================================
    # Per-tier bulkhead acquire timeout (seconds)
    # 0 means fail immediately (zero-wait). To absorb micro-bursts, only
    # critical/standard get a short wait; non_essential stays zero-wait
    # because it is the first to be shed under load.
    # =========================================================================
    tier_critical_bulkhead_timeout_seconds: float = Field(
        default=0.05,
        ge=0.0,
        le=1.0,
        description="Critical tier bulkhead acquire timeout (seconds). 0 means fail immediately.",
    )

    tier_standard_bulkhead_timeout_seconds: float = Field(
        default=0.03,
        ge=0.0,
        le=1.0,
        description="Standard tier bulkhead acquire timeout (seconds). 0 means fail immediately.",
    )

    tier_non_essential_bulkhead_timeout_seconds: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Non_essential tier bulkhead acquire timeout (seconds). Default zero-wait.",
    )

    def get_tier_max_concurrent(self, tier_id: str) -> int:
        """Return the bulkhead max concurrency for the given tier_id."""
        tier_map = {
            "critical": self.tier_critical_max_concurrent,
            "standard": self.tier_standard_max_concurrent,
            "non_essential": self.tier_non_essential_max_concurrent,
        }
        return tier_map.get(tier_id, self.tier_standard_max_concurrent)

    def get_tier_bulkhead_timeout(self, tier_id: str) -> float | None:
        """Return the per-tier bulkhead wait timeout. 0 maps to None (fail fast)."""
        tier_map = {
            "critical": self.tier_critical_bulkhead_timeout_seconds,
            "standard": self.tier_standard_bulkhead_timeout_seconds,
            "non_essential": self.tier_non_essential_bulkhead_timeout_seconds,
        }
        value = tier_map.get(tier_id, 0.0)
        return value if value > 0 else None


def get_admission_control_settings() -> AdmissionControlSettings:
    from baldur.settings.root import get_config

    return get_config().core.admission_control


def reset_admission_control_settings() -> None:
    from baldur.settings.root import get_config

    try:
        del get_config().core.__dict__["admission_control"]
    except KeyError:
        pass
