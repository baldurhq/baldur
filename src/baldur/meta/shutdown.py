"""Meta-Watchdog daemon thread shutdown handler.

Ensures the Watchdog daemon thread is stopped during centralized
shutdown coordination. Uses thread join polling pattern per
EmergencyModeShutdownHandler precedent.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from baldur.core.shutdown_coordinator import ShutdownHandler, TrackedRequest

if TYPE_CHECKING:
    from baldur.interfaces.meta_watchdog import (
        SelfhealerWatchdog as SelfHealerWatchdog,
    )

logger = structlog.get_logger()

__all__ = [
    "WatchdogShutdownHandler",
    "integrate_with_shutdown_coordinator",
]


class WatchdogShutdownHandler(ShutdownHandler):
    """Meta-Watchdog daemon thread shutdown handler."""

    def __init__(self, watchdog: SelfHealerWatchdog) -> None:
        self._watchdog = watchdog

    def on_shutdown_start(self) -> None:
        self._watchdog.stop()

    def is_drain_complete(self) -> bool:
        worker = self._watchdog._worker
        if worker is None or not worker.is_alive():
            return True
        worker.join(timeout=0.1)
        return not worker.is_alive()

    def on_drain_complete(self) -> None:
        pass

    def on_force_shutdown(self, pending_requests: list[TrackedRequest]) -> None:
        self._watchdog.stop()


def integrate_with_shutdown_coordinator() -> WatchdogShutdownHandler | None:
    """Create WatchdogShutdownHandler for external registration."""
    try:
        from baldur.factory.registry import ProviderRegistry

        watchdog = ProviderRegistry.selfhealer_watchdog.safe_get()
        if watchdog is None:
            return None
        return WatchdogShutdownHandler(watchdog)
    except Exception as e:
        logger.debug("meta.watchdog_shutdown_handler_creation_skipped", error=e)
        return None
