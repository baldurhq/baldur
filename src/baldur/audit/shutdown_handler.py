"""Audit subsystem shutdown handler.

Flushes AsyncHealingLogger, AuditSyncWorker, WAL, checkpoint, and
DiskPersistentBuffer via the centralized GracefulShutdownCoordinator.
"""

from __future__ import annotations

import structlog

from baldur.core.shutdown_coordinator import ShutdownHandler, TrackedRequest

logger = structlog.get_logger()

__all__ = ["AuditShutdownHandler"]


class AuditShutdownHandler(ShutdownHandler):
    """Audit subsystem shutdown handler.

    Wraps graceful_shutdown_audit_system() for centralized shutdown
    coordination. The underlying function has its own once-guard to
    prevent double execution when both Gunicorn worker_exit and
    ShutdownCoordinator trigger concurrently.
    """

    def on_shutdown_start(self) -> None:
        pass

    def on_drain_complete(self) -> None:
        self._flush_audit()

    def on_force_shutdown(self, pending_requests: list[TrackedRequest]) -> None:
        self._flush_audit()

    def _flush_audit(self) -> None:
        try:
            from baldur.audit.async_audit_lifecycle import (
                graceful_shutdown_audit_system,
            )

            graceful_shutdown_audit_system()
        except Exception as e:
            logger.warning(
                "audit_shutdown_handler.flush_failed",
                error=e,
            )
