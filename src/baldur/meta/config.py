"""
Meta-Watchdog settings (backward-compatible re-export).

Defined in: baldur.settings.meta_watchdog
"""

from baldur.settings.meta_watchdog import (  # noqa: F401
    MetaWatchdogSettings,
    get_meta_watchdog_settings,
    reset_meta_watchdog_settings,
)

__all__ = [
    "MetaWatchdogSettings",
    "get_meta_watchdog_settings",
    "reset_meta_watchdog_settings",
]
