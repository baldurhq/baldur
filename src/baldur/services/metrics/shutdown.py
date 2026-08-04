"""Metric-collection daemon thread shutdown handler.

Stops the ``DomainGaugeUpdater`` when the shutdown coordinator starts draining,
so the collector does not keep issuing repository reads and emitting log lines
throughout DRAINING/TERMINATING. Mirrors the scaling daemon handlers.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from baldur.core.shutdown_coordinator import ShutdownHandler, TrackedRequest

if TYPE_CHECKING:
    from baldur.services.metrics.periodic_updater import DomainGaugeUpdater

logger = structlog.get_logger()

__all__ = [
    "DomainGaugeUpdaterShutdownHandler",
    "integrate_domain_gauge_updater_with_shutdown_coordinator",
]

#: Liveness-poll budget per drain check — the coordinator calls
#: ``is_drain_complete()`` repeatedly, so each call only needs to be
#: non-blocking, not to wait out a whole tick.
_DRAIN_POLL_TIMEOUT_SECONDS = 0.1


class DomainGaugeUpdaterShutdownHandler(ShutdownHandler):
    """DomainGaugeUpdater daemon thread shutdown handler.

    ``stop()`` interrupts the collector's sleep in milliseconds, but a tick
    already blocked inside a repository read outlives the stop join. Drain
    completion is therefore a ceiling, not a guarantee: an in-flight
    collection reports incomplete until the coordinator's own drain budget
    expires and force-shutdown runs — the same bound every sibling handler has.
    """

    def __init__(self, updater: DomainGaugeUpdater) -> None:
        self._updater = updater

    def on_shutdown_start(self) -> None:
        self._updater.stop()

    def is_drain_complete(self) -> bool:
        thread = self._updater._thread
        if thread is None or not thread.is_alive():
            return True
        thread.join(timeout=_DRAIN_POLL_TIMEOUT_SECONDS)
        return not thread.is_alive()

    def on_drain_complete(self) -> None:
        pass

    def on_force_shutdown(self, pending_requests: list[TrackedRequest]) -> None:
        self._updater.stop()


def integrate_domain_gauge_updater_with_shutdown_coordinator() -> (
    DomainGaugeUpdaterShutdownHandler | None
):
    """Create DomainGaugeUpdaterShutdownHandler for external registration."""
    try:
        from baldur.services.metrics.periodic_updater import get_domain_gauge_updater

        return DomainGaugeUpdaterShutdownHandler(get_domain_gauge_updater())
    except Exception as e:
        logger.debug(
            "metrics.domain_gauge_updater_shutdown_handler_creation_skipped", error=e
        )
        return None
