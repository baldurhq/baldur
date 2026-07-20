"""
X-Test Resource Guard Settings - Pydantic v2.

Settings that check system CPU/memory overload on an X-Test request, to avoid
placing extra burden on the production system.

Environment Variables:
    BALDUR_RESOURCE_GUARD_CPU_THRESHOLD=80           # CPU threshold (%)
    BALDUR_RESOURCE_GUARD_MEMORY_THRESHOLD=85        # Memory threshold (%)
    BALDUR_RESOURCE_GUARD_RESOURCE_CHECK_ENABLED=true  # Enable resource check
    BALDUR_RESOURCE_GUARD_RETRY_AFTER_SECONDS=30     # Retry-After for a 429
"""

from pydantic import Field
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config


class ResourceGuardSettings(BaseSettings):
    """
    X-Test resource guard settings.

    Blocks X-Test requests while system CPU/memory is overloaded, protecting
    the stability of the production system.

    Kept consistent with RecoveryGate's cpu_threshold_percent (80%).
    """

    model_config = make_settings_config("BALDUR_RESOURCE_GUARD_")

    # ==========================================================================
    # CPU threshold
    # ==========================================================================
    cpu_threshold: float = Field(
        default=80.0,
        ge=50.0,
        le=99.0,
        description="CPU usage threshold (%). Blocks X-Test when exceeded. Aligned with RecoveryGate at 80%.",
    )

    # ==========================================================================
    # Memory threshold
    # ==========================================================================
    memory_threshold: float = Field(
        default=85.0,
        ge=50.0,
        le=99.0,
        description="Memory usage threshold (%). Blocks X-Test when exceeded.",
    )

    # ==========================================================================
    # Resource check enablement
    # ==========================================================================
    resource_check_enabled: bool = Field(
        default=True,
        description="Enable resource check. Skips check when false.",
    )

    # ==========================================================================
    # Recommended wait time for a 429 response
    # ==========================================================================
    retry_after_seconds: int = Field(
        default=30,
        ge=10,
        le=300,
        description="Retry-After header value (seconds) for 429 responses.",
    )


def get_resource_guard_settings() -> "ResourceGuardSettings":
    from baldur.settings.root import get_config

    return get_config().meta.resource_guard


def reset_resource_guard_settings() -> None:
    from baldur.settings.root import get_config

    try:
        del get_config().meta.__dict__["resource_guard"]
    except KeyError:
        pass
