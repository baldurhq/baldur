"""
Chaos Blast Radius Settings - Pydantic v2.

Blast radius control settings for chaos experiments.

Replaces:
- services/chaos/blast_radius.py:BlastRadiusPolicy (hardcoded defaults)
- services/chaos/base/models.py:max_traffic_percent settings

Environment Variables:
    BALDUR_CHAOS_BLAST_RADIUS_INSTANCE_MAX_CONCURRENT=5
    BALDUR_CHAOS_BLAST_RADIUS_SERVICE_MAX_CONCURRENT=2
    BALDUR_CHAOS_BLAST_RADIUS_REGION_MAX_CONCURRENT=1
    BALDUR_CHAOS_BLAST_RADIUS_MAX_TRAFFIC_PERCENT_SERVICE=50.0
    BALDUR_CHAOS_BLAST_RADIUS_MAX_TRAFFIC_PERCENT_REGION=10.0
"""

import structlog
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config
from baldur.settings.field_types import (
    Percentage,
    SmallCount,
    TinyCount,
)
from baldur.settings.validators import warn_above

logger = structlog.get_logger()


class ChaosBlastRadiusSettings(BaseSettings):
    """
    Chaos blast radius settings.

    Bounds the impact scope of a chaos experiment to keep it safe.

    Levels:
    - INSTANCE: a single pod/instance (lowest risk)
    - SERVICE: the whole service (medium risk)
    - REGION: the whole region/AZ (highest risk, approval required)
    """

    model_config = make_settings_config("BALDUR_CHAOS_BLAST_RADIUS_")

    # ==========================================================================
    # Concurrent Limits (from blast_radius.py)
    # ==========================================================================
    instance_max_concurrent: SmallCount = Field(
        default=5,
        description="Maximum concurrent experiments at INSTANCE level",
    )

    service_max_concurrent: TinyCount = Field(
        default=2,
        description="Maximum concurrent experiments at SERVICE level",
    )

    region_max_concurrent: int = Field(
        default=1,
        ge=1,
        le=3,
        description="Maximum concurrent experiments at REGION level (CRITICAL)",
    )

    # ==========================================================================
    # Auto-Approval (from blast_radius.py)
    # ==========================================================================
    instance_auto_approve: bool = Field(
        default=True,
        description="Auto-approve INSTANCE level experiments",
    )

    service_auto_approve: bool = Field(
        default=False,
        description="Auto-approve SERVICE level experiments",
    )

    region_auto_approve: bool = Field(
        default=False,
        description="Auto-approve REGION level experiments (always False recommended)",
    )

    # ==========================================================================
    # Time-based Restrictions (from blast_radius.py)
    # ==========================================================================
    allowed_hours_start: int = Field(
        default=2,
        ge=0,
        le=23,
        description="Experiment allowed start hour (UTC, default 02:00)",
    )

    allowed_hours_end: int = Field(
        default=6,
        ge=0,
        le=23,
        description="Experiment allowed end hour (UTC, default 06:00)",
    )

    allow_outside_window: bool = Field(
        default=False,
        description="Allow experiments outside maintenance window",
    )

    # ==========================================================================
    # Traffic Restrictions (from blast_radius.py)
    # ==========================================================================
    max_traffic_percent_instance: Percentage = Field(
        default=100.0,
        description="Maximum traffic impact at INSTANCE level (%)",
    )

    max_traffic_percent_service: Percentage = Field(
        default=50.0,
        description="Maximum traffic impact at SERVICE level (%)",
    )

    max_traffic_percent_region: float = Field(
        default=10.0,
        ge=0.0,
        le=50.0,
        description="Maximum traffic impact at REGION level (%, capped at 50%)",
    )

    # ==========================================================================
    # Safety Limits (from blast_radius.py)
    # ==========================================================================
    excluded_services: list[str] = Field(
        default_factory=list,
        description="List of services excluded from experiments",
    )

    excluded_domains: list[str] = Field(
        default_factory=list,
        description="List of domains excluded from experiments",
    )

    @field_validator("region_auto_approve")
    @classmethod
    def warn_region_auto_approve(cls, v: bool) -> bool:
        """Auto-approving REGION level experiments is dangerous."""
        if v:
            logger.warning("chaos_blast_radius.region_auto_approve_dangerous")
        return v

    @field_validator("max_traffic_percent_region")
    @classmethod
    def _warn_high_region_traffic(cls, v: float) -> float:
        """Warn when REGION level traffic impact is high."""
        return warn_above(20.0, "chaos_blast_radius.region_traffic_percent_high")(v)


# =============================================================================
# Singleton Pattern
# =============================================================================


def get_chaos_blast_radius_settings() -> "ChaosBlastRadiusSettings":
    """Get cached ChaosBlastRadiusSettings instance."""
    from baldur.settings.root import get_config

    return get_config().services_group.chaos_blast_radius


def reset_chaos_blast_radius_settings() -> None:
    """Reset cached settings (for testing)."""
    from baldur.settings.root import get_config

    try:
        del get_config().services_group.__dict__["chaos_blast_radius"]
    except KeyError:
        pass
