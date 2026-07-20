"""
Auto-Scaling & Backpressure settings (backward-compatible re-export).

Actual definitions: baldur.settings.backpressure
"""

from baldur.settings.backpressure import (  # noqa: F401
    LEVEL_RATE_MULTIPLIERS,
    BackpressureLevel,
    BackpressureSettings,
    BackpressureStrategy,
    get_backpressure_settings,
    reset_backpressure_settings,
)

__all__ = [
    "BackpressureLevel",
    "BackpressureStrategy",
    "LEVEL_RATE_MULTIPLIERS",
    "BackpressureSettings",
    "get_backpressure_settings",
    "reset_backpressure_settings",
]
