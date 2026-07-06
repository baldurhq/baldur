"""
Bridge Settings - Pydantic v2.

Configuration for Baldur's bridge adapters that integrate third-party
resilience libraries (currently tenacity; pybreaker reserved for follow-up).

Environment Variables:
    BALDUR_BRIDGE_TENACITY_ENABLED=false
    BALDUR_BRIDGE_TENACITY_INSTRUMENT=false
    BALDUR_BRIDGE_TENACITY_METRICS=true

Reference:
    451 - D8
"""

from pydantic import Field
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config


class BridgeSettings(BaseSettings):
    """Toggles for Baldur's third-party library bridge adapters.

    All flags default to off so OSS users opt in explicitly. The settings
    object is consulted at bootstrap time and at policy construction time.
    """

    model_config = make_settings_config("BALDUR_BRIDGE_")

    tenacity_enabled: bool = Field(
        default=False,
        description="Master toggle for the tenacity bridge. When False, "
        "TenacityBridgePolicy still constructs but instrument_tenacity() is "
        "skipped at bootstrap.",
    )
    tenacity_instrument: bool = Field(
        default=False,
        description="When True (and tenacity_enabled is True), bootstrap "
        "monkey-patches tenacity.Retrying.__init__ to attach Baldur metric "
        "and audit callbacks to every Retrying instance created afterwards.",
    )
    tenacity_metrics: bool = Field(
        default=True,
        description="Emit Prometheus metrics from the tenacity bridge "
        "(attempt counts, retry-exhausted events). Off disables metric "
        "emission only; budget and rate-limit guards are unaffected.",
    )


# =============================================================================
# Singleton Pattern (standalone — not part of any settings group)
# =============================================================================


def get_bridge_settings() -> BridgeSettings:
    """Return the cached BridgeSettings singleton."""
    from baldur.runtime import get_runtime

    return get_runtime().get_settings(BridgeSettings)


def reset_bridge_settings() -> None:
    """Reset the BridgeSettings singleton — for test isolation."""
    from baldur.runtime import get_runtime

    get_runtime().reset_settings(BridgeSettings)
