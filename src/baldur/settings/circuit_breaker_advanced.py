"""
Circuit Breaker Advanced Protection Settings - Pydantic v2.

Single Source of Truth for circuit breaker advanced protection.
"""

from pydantic import Field
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config
from baldur.settings.field_types import Percentage


class CircuitBreakerAdvancedSettings(BaseSettings):
    """Circuit Breaker advanced protection settings."""

    model_config = make_settings_config("BALDUR_CB_ADVANCED_")

    # Global enable — gates the panic-threshold escalation lane. The chaos
    # safety pre-check reads the panic probe regardless: a guard that decides
    # whether an experiment may run has no reason to be switchable.
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


def get_circuit_breaker_advanced_settings() -> "CircuitBreakerAdvancedSettings":
    from baldur.settings.root import get_config

    return get_config().core.circuit_breaker_advanced


def reset_circuit_breaker_advanced_settings() -> None:
    from baldur.settings.root import get_config

    try:
        del get_config().core.__dict__["circuit_breaker_advanced"]
    except KeyError:
        pass
