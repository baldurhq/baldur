"""
Ring Buffer Settings - Pydantic v2.

Ring Buffer settings for Shadow Logging.
Following the non-intrusive principle, DROP_OLDEST is the default and the main
application's performance is never affected.

Source:
- audit/ring_buffer.py

Environment Variables:
    BALDUR_RING_BUFFER_CAPACITY=10000
    BALDUR_RING_BUFFER_BATCH_MAX_SIZE=100
    BALDUR_RING_BUFFER_STRATEGY=drop_oldest
"""

from typing import Literal

import structlog
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config

logger = structlog.get_logger()


class RingBufferSettings(BaseSettings):
    """
    Ring Buffer settings.

    Defines the non-intrusive buffer settings for Shadow Logging.
    Never blocks the main application.
    """

    model_config = make_settings_config("BALDUR_RING_BUFFER_")

    # ==========================================================================
    # Buffer Settings (from ring_buffer.py line 67)
    # ==========================================================================
    capacity: int = Field(
        default=10000,
        ge=100,
        le=1000000,
        description="Ring Buffer maximum capacity",
    )

    # ==========================================================================
    # Batch Settings (from ring_buffer.py - get_batch default)
    # ==========================================================================
    batch_max_size: int = Field(
        default=100,
        ge=1,
        le=10000,
        description="Maximum items per batch processing",
    )

    # ==========================================================================
    # Strategy Settings (from ring_buffer.py BackpressureStrategy)
    # ==========================================================================
    strategy: Literal["drop_oldest", "drop_newest"] = Field(
        default="drop_oldest",
        description="Backpressure strategy. drop_oldest (recommended: non-intrusive) or drop_newest.",
    )

    @field_validator("capacity")
    @classmethod
    def validate_capacity(cls, v: int) -> int:
        """Warn when capacity is too large."""
        if v > 100000:
            logger.warning(
                "ring_buffer_settings.high_consider_using_memory",
                setting_value=v,
            )
        return v


# =============================================================================
# Singleton Pattern
# =============================================================================


def get_ring_buffer_settings() -> "RingBufferSettings":
    """
    Return the cached RingBufferSettings instance.

    Returns:
        RingBufferSettings: The singleton instance
    """
    from baldur.settings.root import get_config

    return get_config().scaling.ring_buffer


def reset_ring_buffer_settings() -> None:
    """
    Reset the cached settings (for tests).

    Call this to reload the settings after changing environment variables.
    """
    from baldur.settings.root import get_config

    try:
        del get_config().scaling.__dict__["ring_buffer"]
    except KeyError:
        pass
