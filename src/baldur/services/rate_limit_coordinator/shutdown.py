"""Cooldown-announcer daemon thread shutdown handler.

Stops the coordinator's ``CooldownAnnouncer`` when the shutdown coordinator
starts draining, so the announcer does not keep reading the shared rate-limit
store and emitting all-clears throughout DRAINING/TERMINATING. Mirrors the
metric-collection daemon handler.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from baldur.core.shutdown_coordinator import ShutdownHandler, TrackedRequest

if TYPE_CHECKING:
    from baldur.services.rate_limit_coordinator.announcer import CooldownAnnouncer

logger = structlog.get_logger()

__all__ = [
    "CooldownAnnouncerShutdownHandler",
    "integrate_cooldown_announcer_with_shutdown_coordinator",
]

#: Liveness-poll budget per drain check — the coordinator calls
#: ``is_drain_complete()`` repeatedly, so each call only needs to be
#: non-blocking, not to wait out a whole verification pass.
_DRAIN_POLL_TIMEOUT_SECONDS = 0.1


class CooldownAnnouncerShutdownHandler(ShutdownHandler):
    """Cooldown-announcer daemon thread shutdown handler.

    ``stop()`` interrupts the announcer's sleep in milliseconds, but a pass
    already blocked inside a store read outlives the stop join. Drain completion
    is therefore a ceiling, not a guarantee — the same bound every sibling
    handler has. What the stop *does* guarantee is that such a pass announces
    nothing when it returns: the records are cleared and the announcement
    permission is withdrawn before the join.
    """

    def __init__(self, announcer: CooldownAnnouncer | None = None) -> None:
        """
        Args:
            announcer: the announcer to stop. Left ``None`` in production, where
                the handler is built during ``init()`` — which never constructs
                the coordinator. Resolving it here would *create* a coordinator
                (and its storage backend) in every process, including the ones
                that never make a coordinated call. Tests pass an explicit
                instance.
        """
        self._announcer = announcer

    def _resolve(self) -> CooldownAnnouncer | None:
        """The live announcer, or ``None`` when no coordinator was ever built.

        Deliberately reads ``RateLimitCoordinator._instance`` rather than calling
        ``get_instance()``: building a coordinator during shutdown to stop the
        thread it does not have is pure cost.
        """
        if self._announcer is not None:
            return self._announcer

        from baldur.services.rate_limit_coordinator.coordinator import (
            RateLimitCoordinator,
        )

        instance = RateLimitCoordinator._instance
        return None if instance is None else instance._announcer

    def on_shutdown_start(self) -> None:
        announcer = self._resolve()
        if announcer is not None:
            announcer.stop()

    def is_drain_complete(self) -> bool:
        announcer = self._resolve()
        if announcer is None:
            return True
        thread = announcer._state.thread
        if thread is None or not thread.is_alive():
            return True
        thread.join(timeout=_DRAIN_POLL_TIMEOUT_SECONDS)
        return not thread.is_alive()

    def on_drain_complete(self) -> None:
        pass

    def on_force_shutdown(self, pending_requests: list[TrackedRequest]) -> None:
        announcer = self._resolve()
        if announcer is not None:
            announcer.stop()


def integrate_cooldown_announcer_with_shutdown_coordinator() -> (
    CooldownAnnouncerShutdownHandler | None
):
    """Create CooldownAnnouncerShutdownHandler for external registration.

    Deliberately does not touch the coordinator singleton: shutdown handlers are
    registered during ``init()``, which never builds a coordinator, and building
    one here would give every process a rate-limit storage backend it may never
    use. The handler resolves the announcer at shutdown instead.
    """
    try:
        return CooldownAnnouncerShutdownHandler()
    except Exception as e:
        logger.debug(
            "rate_limit_coordinator.announcer_shutdown_handler_creation_skipped",
            error=e,
        )
        return None
