"""DLQ Outbox Graceful Shutdown Integration.

The outbox buffers captured failures in a process-local RingBuffer drained by a
daemon thread. A daemon thread is killed at interpreter exit without running, so
whatever the buffer still holds at that moment is lost — the buffer has no WAL
and no on-disk spill. This handler is the outbox's seat at the shutdown table:
it holds the coordinator's drain open while the writer can still empty the
buffer through the real DLQ path, and hands the remainder to the teardown, which
spills it to the local fallback tier.

Not a substitute for the adapters' own exit hooks. The coordinator drain runs
only on a signalled exit; a worker recycle (``max_requests``,
``maxtasksperchild``) never initiates one, and there each adapter's exit hook
calls the same idempotent teardown directly.
"""

from __future__ import annotations

import structlog

from baldur.core.shutdown_coordinator import ShutdownHandler, TrackedRequest

logger = structlog.get_logger()

__all__ = [
    "DLQOutboxShutdownHandler",
    "integrate_with_shutdown_coordinator",
]


class DLQOutboxShutdownHandler(ShutdownHandler):
    """Graceful shutdown handler for the DLQ outbox."""

    def on_shutdown_start(self) -> None:
        """Deliberately does nothing — the writer keeps draining.

        Unlike the precomputed-cache handler, this one does NOT stop its worker
        here. Entries captured *during* the drain — by the very in-flight
        requests the coordinator is waiting on — must still reach the DLQ, and
        stopping the drainer at shutdown start would strand exactly those.
        """

    def is_drain_complete(self) -> bool:
        """True once waiting can no longer change the outcome.

        Three ways that happens: there is no outbox in this process, the buffer
        is empty with nothing mid-write, or waiting cannot help — the drainer is
        not alive, or it is in sustained backoff. Both halves of that last arm
        mean the buffer will not empty by waiting, and the teardown spills the
        remainder either way. Without it a dead drainer would hold this False
        for the coordinator's entire drain window on every shutdown.
        """
        try:
            from baldur.services.dlq_outbox import outbox as outbox_module

            outbox = outbox_module._outbox
            if outbox is None:
                return True

            worker = outbox.worker
            if not worker.is_alive or worker.is_backing_off:
                return True

            return outbox.buffer.size == 0 and worker.in_flight == 0
        except Exception as exc:
            # Undecidable: report drained rather than hold the whole shutdown
            # open on a handler that cannot read its own state.
            logger.debug("dlq_outbox_shutdown.drain_check_failed", error=exc)
            return True

    def on_drain_complete(self) -> None:
        self._stop_outbox()

    def on_force_shutdown(self, pending_requests: list[TrackedRequest]) -> None:
        self._stop_outbox()

    def _stop_outbox(self) -> None:
        try:
            from baldur.services.dlq_outbox.outbox import stop_outbox_for_shutdown

            result = stop_outbox_for_shutdown()
            logger.info(
                "dlq_outbox.shutdown_teardown_completed",
                pending_at_entry=result.pending_at_entry,
                dispatched=result.dispatched,
                soft_failed=result.soft_failed,
                failed=result.failed,
                emergency_dumped=result.emergency_dumped,
                residual=result.residual,
                duplicated=result.duplicated,
            )
        except Exception as exc:
            logger.warning("dlq_outbox_shutdown.teardown_failed", error=exc)


def integrate_with_shutdown_coordinator() -> DLQOutboxShutdownHandler | None:
    """Create DLQOutboxShutdownHandler for external registration."""
    try:
        return DLQOutboxShutdownHandler()
    except Exception as e:
        logger.debug("dlq_outbox_shutdown.handler_creation_skipped", error=e)
        return None
