"""
Capacity Reservation Settings - Pydantic v2.

Settings for pre-provisioning capacity ahead of scheduled events.

Environment variable prefix: BALDUR_CAPACITY_RESERVATION_
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config


class CapacityReservationSettings(BaseSettings):
    """
    Capacity Reservation settings.

    Environment Variables:
        BALDUR_CAPACITY_RESERVATION_ENABLED=false
        BALDUR_CAPACITY_RESERVATION_DEFAULT_WARMUP_MINUTES=5
        BALDUR_CAPACITY_RESERVATION_DRY_RUN=true
        ...
    """

    model_config = make_settings_config("BALDUR_CAPACITY_RESERVATION_")

    enabled: bool = Field(
        default=False,
        description="Enable/disable capacity reservation service",
    )

    default_warmup_minutes: int = Field(
        default=5,
        ge=1,
        le=60,
        description="Default pre-warming time (minutes)",
    )

    scheduler_interval_seconds: int = Field(
        default=30,
        ge=5,
        le=300,
        description="Scheduler check interval (seconds)",
    )

    max_rate_multiplier: float = Field(
        default=5.0,
        ge=1.0,
        le=20.0,
        description="Maximum rate scaling multiplier",
    )

    max_pool_multiplier: float = Field(
        default=3.0,
        ge=1.0,
        le=10.0,
        description="Maximum pool scaling multiplier",
    )

    max_bulkhead_extra_permits: int = Field(
        default=100,
        ge=0,
        le=1000,
        description="Maximum additional bulkhead permits",
    )

    cooldown_grace_period_seconds: int = Field(
        default=300,
        ge=60,
        le=3600,
        description="Grace period before restoring settings after event ends (seconds)",
    )

    max_concurrent_events: int = Field(
        default=3,
        ge=1,
        le=10,
        description="Maximum number of concurrent events",
    )

    dry_run: bool = Field(
        default=True,
        description="When True, only logs without applying actual adjustments",
    )

    safety_valve_cpu_threshold: float = Field(
        default=0.95,
        ge=0.5,
        le=1.0,
        description="Safety valve CPU threshold. Immediately switches to CRITICAL when exceeded, even in event mode",
    )

    safety_valve_error_rate_threshold: float = Field(
        default=0.10,
        ge=0.01,
        le=1.0,
        description="Safety valve error rate threshold",
    )

    safety_valve_min_hold_seconds: int = Field(
        default=120,
        ge=30,
        le=600,
        description="Minimum hold time after safety valve activation (prevents flapping)",
    )


def get_capacity_reservation_settings() -> CapacityReservationSettings:
    from baldur.settings.root import get_config

    return get_config().services_group.capacity_reservation


def reset_capacity_reservation_settings() -> None:
    from baldur.settings.root import get_config

    try:
        del get_config().services_group.__dict__["capacity_reservation"]
    except KeyError:
        pass
