"""
Meta-Watchdog 설정 (하위 호환 re-export).

실제 정의: baldur.settings.meta_watchdog
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
