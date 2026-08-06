"""
Runtime Config Watch Settings - Pydantic v2.

Cadence of the per-process poll that carries a stored configuration change to
already-running workers. The interval is not an implementation detail: it *is*
the convergence bound the runtime-apply declaration reports to an operator, so
raising it widens the window a fleet can serve two different configurations for
one service.

Environment Variables:
    BALDUR_RUNTIME_CONFIG_WATCH_INTERVAL_SECONDS=30
"""

from pydantic import Field
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config


class RuntimeConfigWatchSettings(BaseSettings):
    """Runtime configuration delivery poll settings."""

    model_config = make_settings_config("BALDUR_RUNTIME_CONFIG_WATCH_")

    interval_seconds: int = Field(
        default=30,
        ge=0,
        le=3600,
        description=(
            "How often each process re-reads the stored configuration of every "
            "domain that registered an invalidation target, in seconds. This "
            "value is reported verbatim as the delivery's convergence bound, "
            "so a change stored just after one read reaches consumers by the "
            "next one. 0 disables the poll — the domain then reports itself as "
            "stored-only rather than claiming a bound it cannot keep."
        ),
    )


def get_runtime_config_watch_settings() -> "RuntimeConfigWatchSettings":
    """Return cached RuntimeConfigWatchSettings via RootConfig."""
    from baldur.settings.root import get_config

    return get_config().services_group.runtime_config_watch


def reset_runtime_config_watch_settings() -> None:
    """Reset cached RuntimeConfigWatchSettings (for testing)."""
    from baldur.settings.root import get_config

    try:
        del get_config().services_group.__dict__["runtime_config_watch"]
    except KeyError:
        pass
