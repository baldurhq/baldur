"""
CgroupResourceMonitor Settings - Pydantic v2.

Container/VM resource monitoring settings.
Memory/CPU safety margins are configurable through environment variables.

Environment Variables:
    BALDUR_RESOURCE_MONITOR_SAFETY_MARGIN=0.15
    BALDUR_RESOURCE_MONITOR_CPU_MARGIN=0.10
"""

from pydantic import Field
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config


class ResourceMonitorSettings(BaseSettings):
    """
    CgroupResourceMonitor settings.

    Sets the resource usage margins that keep a Chaos Experiment's Resource
    Exhaustion within safe limits.
    """

    model_config = make_settings_config("BALDUR_RESOURCE_MONITOR_")

    # ==========================================================================
    # Memory safety margin
    # ==========================================================================
    safety_margin: float = Field(
        default=0.15,
        ge=0.05,
        le=0.5,
        description="Memory usage safety margin (0.15 = 15%). Headroom to prevent OOM Killer.",
    )

    # ==========================================================================
    # CPU safety margin (for future expansion)
    # ==========================================================================
    cpu_margin: float = Field(
        default=0.10,
        ge=0.05,
        le=0.5,
        description="CPU usage safety margin (0.10 = 10%). For future CPU limit monitoring.",
    )


def get_resource_monitor_settings() -> "ResourceMonitorSettings":
    from baldur.settings.root import get_config

    return get_config().resilience.resource_monitor


def reset_resource_monitor_settings() -> None:
    from baldur.settings.root import get_config

    try:
        del get_config().resilience.__dict__["resource_monitor"]
    except KeyError:
        pass
