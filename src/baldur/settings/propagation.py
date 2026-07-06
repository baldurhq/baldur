"""
Propagation Settings - Cross-Cluster Configuration Propagation.

Defines propagation consistency settings.

Reference: docs/baldur/middleware_system/70_MULTI_CLUSTER_ARCHITECTURE.md
"""

from pydantic import Field
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config
from baldur.settings.field_types import Probability


class PropagationSettings(BaseSettings):
    """
    Propagation consistency settings.

    Data consistency tiers:
    - Tier 1: Audit, Governance, Emergency (propagation within 1 second)
    - Tier 2: Metrics, Stats, Cache (propagation within 30 seconds)
    """

    model_config = make_settings_config("BALDUR_PROPAGATION_")

    # Tier 1 SLA (immediate propagation)
    tier1_max_latency_ms: int = Field(
        default=1000,
        description="Tier 1 (Audit/Governance/Emergency) max propagation latency (ms)",
        ge=100,
        le=5000,
    )

    # Tier 2 SLA (eventual consistency)
    tier2_max_latency_ms: int = Field(
        default=30000,
        description="Tier 2 (Metrics/Stats) max propagation latency (ms)",
        ge=1000,
        le=300000,
    )

    # Auto listener start
    auto_start_listener: bool = Field(
        default=False,
        description="Auto-start propagation listener on application start",
    )

    # Retry settings
    retry_count: int = Field(
        default=3,
        description="Retry count on propagation failure",
        ge=0,
        le=10,
    )
    retry_delay_ms: int = Field(
        default=500,
        description="Retry interval (ms)",
        ge=100,
        le=10000,
    )

    # Health Score weight
    health_score_weight: Probability = Field(
        default=0.3,
        description="Propagation score weight in composite HealthScore (0.0-1.0)",
    )

    # Penalty settings
    tier1_penalty_points: int = Field(
        default=5,
        description="Penalty points for Tier 1 SLA violation",
        ge=1,
        le=50,
    )
    tier2_penalty_points: int = Field(
        default=1,
        description="Penalty points for Tier 2 SLA violation",
        ge=1,
        le=10,
    )


def get_propagation_settings() -> "PropagationSettings":
    """Return PropagationSettings singleton."""
    from baldur.settings.root import get_config

    return get_config().multi_region.propagation


def reset_propagation_settings() -> None:
    """Reset singleton for testing."""
    from baldur.settings.root import get_config

    try:
        del get_config().multi_region.__dict__["propagation"]
    except KeyError:
        pass
