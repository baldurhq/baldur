"""
Circuit Breaker Advanced Protection Settings - Pydantic v2.

Single Source of Truth for circuit breaker advanced protection.
Replaces: core/config.py:CircuitBreakerAdvancedConfig (lines 540-605)
"""

from pydantic import Field
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config
from baldur.settings.field_types import (
    IntervalDuration,
    MediumCount,
    Percentage,
)


class CircuitBreakerAdvancedSettings(BaseSettings):
    """Circuit Breaker advanced protection settings."""

    model_config = make_settings_config("BALDUR_CB_ADVANCED_")

    # Global enable
    enabled: bool = Field(
        default=False,
        description="Enable/disable advanced protection features",
    )

    # =========================================================================
    # Load Shedding
    # =========================================================================
    load_shedding_enabled: bool = Field(
        default=False,
        description="Enable load shedding",
    )
    load_shedding_trigger_threshold: Percentage = Field(
        default=30.0,
        description="Load shedding trigger threshold (%)",
    )

    # =========================================================================
    # Adaptive Threshold (Emergency Level integration)
    # =========================================================================
    adaptive_base_failure_threshold: MediumCount = Field(
        default=5,
        description="Base failure count threshold",
    )
    adaptive_base_window_seconds: IntervalDuration = Field(
        default=60,
        description="Base observation window (seconds)",
    )

    # =========================================================================
    # Blast Radius integration
    # =========================================================================
    blast_radius_integration: bool = Field(
        default=True,
        description="Enable blast radius integration",
    )
    blast_radius_block_on_critical: bool = Field(
        default=True,
        description="Block auto OPEN on CRITICAL",
    )

    # =========================================================================
    # Freeze Mode
    # =========================================================================
    freeze_on_lockdown: bool = Field(
        default=True,
        description="Enable freeze mode on LOCKDOWN",
    )
    allow_manual_override_in_lockdown: bool = Field(
        default=True,
        description="Allow manual override during LOCKDOWN",
    )

    # =========================================================================
    # Panic Threshold
    # =========================================================================
    panic_threshold_percent: Percentage = Field(
        default=70.0,
        description="OPEN CB ratio threshold (Panic if >= 70%)",
    )
    panic_threshold_action: str = Field(
        default="freeze",
        description='Action on panic ("freeze" | "alert_only")',
    )

    # =========================================================================
    # Open Strategy
    # =========================================================================
    default_open_strategy: str = Field(
        default="immediate",
        description='Open strategy ("immediate" | "graceful")',
    )
    graceful_drain_timeout_seconds: int = Field(
        default=30,
        ge=1,
        le=300,
        description="Drain timeout for graceful open (seconds)",
    )


def get_circuit_breaker_advanced_settings() -> "CircuitBreakerAdvancedSettings":
    from baldur.settings.root import get_config

    return get_config().core.circuit_breaker_advanced


def reset_circuit_breaker_advanced_settings() -> None:
    from baldur.settings.root import get_config

    try:
        del get_config().core.__dict__["circuit_breaker_advanced"]
    except KeyError:
        pass
