"""
Thread Management Settings — Pydantic v2.

Thread join and background worker timeout settings.
Makes the previously hardcoded thread.join(timeout=N) values controllable
through environment variables.

Environment Variables:
    BALDUR_THREAD_MANAGEMENT_JOIN_TIMEOUT=5.0
    BALDUR_THREAD_MANAGEMENT_JOIN_TIMEOUT_LONG=10.0
"""

from pydantic import Field
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config


class ThreadManagementSettings(BaseSettings):
    """Thread join and background worker timeout settings."""

    model_config = make_settings_config("BALDUR_THREAD_MANAGEMENT_")

    join_timeout: float = Field(
        default=5.0,
        ge=1.0,
        le=60.0,
        description="Default timeout for thread.join() calls",
    )

    join_timeout_long: float = Field(
        default=10.0,
        ge=5.0,
        le=120.0,
        description="Timeout for long-running thread joins (event bus, capacity reservation)",
    )


def get_thread_management_settings() -> "ThreadManagementSettings":
    """Single entry point via the root settings (SSOT)."""
    from baldur.settings.root import get_config

    return get_config().core.thread_management


def reset_thread_management_settings() -> None:
    """Delegate to the root reset (for tests)."""
    from baldur.settings.root import get_config

    try:
        del get_config().core.__dict__["thread_management"]
    except KeyError:
        pass
