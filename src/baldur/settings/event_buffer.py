"""
Event Buffer Settings - Pydantic v2.

Per-request audit event buffer size settings.
Prevents event loss during large bulk operations.

Environment Variables:
    BALDUR_EVENT_BUFFER_MAX_EVENTS_PER_REQUEST=1000
    BALDUR_EVENT_BUFFER_WARNING_THRESHOLD=0.8
    BALDUR_EVENT_BUFFER_OVERFLOW_STRATEGY=drop_oldest
"""

from typing import Literal

import structlog
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config

logger = structlog.get_logger()


class EventBufferSettings(BaseSettings):
    """
    Per-request event buffer settings.

    Manages the buffer size for audit events collected per HTTP request.
    Prevents event loss during bulk operations (mass create/update/delete).
    """

    model_config = make_settings_config("BALDUR_EVENT_BUFFER_")

    # ==========================================================================
    # Per-Request Buffer Limit
    # ==========================================================================
    max_events_per_request: int = Field(
        default=1000,
        ge=10,
        le=100000,
        description=(
            "Maximum audit event buffer size per request. "
            "10,000+ recommended for bulk operations. "
            "When using RingBuffer, this value is replaced by RingBuffer capacity."
        ),
    )

    # ==========================================================================
    # Warning Threshold
    # ==========================================================================
    warning_threshold: float = Field(
        default=0.8,
        ge=0.5,
        le=0.95,
        description="Buffer usage warning threshold (80%)",
    )

    # ==========================================================================
    # Overflow Strategy
    # ==========================================================================
    overflow_strategy: Literal["drop_oldest", "drop_newest", "block"] = Field(
        default="drop_oldest",
        description=(
            "Buffer overflow strategy. "
            "drop_oldest: discard oldest events (recommended). "
            "drop_newest: discard new events. "
            "block: block until buffer space is available (not recommended)."
        ),
    )

    @field_validator("overflow_strategy")
    @classmethod
    def validate_overflow_strategy(cls, v: str) -> str:
        """Warn when the block strategy is used."""
        if v == "block":
            logger.warning(
                "event_buffer.blocking_overflow_strategy_discouraged",
                recommended_strategy="drop_oldest",
            )
        return v


# ==========================================================================
# Singleton management
# ==========================================================================
def get_event_buffer_settings() -> "EventBufferSettings":
    """Get cached EventBufferSettings instance."""
    from baldur.runtime import get_runtime

    return get_runtime().get_settings(EventBufferSettings)


def reset_event_buffer_settings() -> None:
    """Reset cached settings (for testing)."""
    from baldur.runtime import get_runtime

    get_runtime().reset_settings(EventBufferSettings)
