"""
Precomputed Cache Graceful Shutdown Integration.

Stops the precomputed cache background worker thread via
stop_precomputed_cache().
"""

from __future__ import annotations

import structlog

from baldur.core.shutdown_coordinator import ShutdownHandler, TrackedRequest

logger = structlog.get_logger()

__all__ = [
    "PrecomputedCacheShutdownHandler",
    "integrate_with_shutdown_coordinator",
]


class PrecomputedCacheShutdownHandler(ShutdownHandler):
    """Graceful shutdown handler for the precomputed cache worker."""

    def on_shutdown_start(self) -> None:
        """Stop the precomputed cache worker."""
        try:
            from baldur.services.precomputed_cache.worker import (
                stop_precomputed_cache,
            )

            stop_precomputed_cache()
            logger.info("precomputed_cache_shutdown.worker_stopped")
        except Exception as exc:
            logger.debug("precomputed_cache_shutdown.worker_stop_failed", error=exc)

    def on_drain_complete(self) -> None:
        """No additional action needed — worker already stopped."""
        pass

    def on_force_shutdown(self, pending_requests: list[TrackedRequest]) -> None:
        """Force shutdown — same as start (idempotent)."""
        self.on_shutdown_start()


def integrate_with_shutdown_coordinator() -> PrecomputedCacheShutdownHandler | None:
    """Create PrecomputedCacheShutdownHandler for external registration."""
    try:
        return PrecomputedCacheShutdownHandler()
    except Exception as e:
        logger.debug("precomputed_cache_shutdown.handler_creation_skipped", error=e)
        return None
