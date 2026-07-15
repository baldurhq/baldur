"""OSS DLQ read + single-entry-action backing.

``DLQReadService`` is the OSS-tier DLQ read/visibility core — it lists and
inspects captured entries (list / detail / facets / stats / cleanup stats) and
drives single-entry recovery actions (retry / resolve / force-redrive) over the
same repository read primitives the PRO ``DLQService`` uses. It IS-A
``DLQCaptureService`` (inherits ``repository`` / ``config`` / ``is_enabled`` /
``_log_dlq_audit`` / ``store_failure`` / local fallback), so the single-entry
actions find ``_execute_replay`` / ``_emit_replay_exhausted`` / ``resolve_entry``
/ ``self.repository`` / ``self.config`` all on one object.

The PRO ``DLQService`` inherits these read + single-entry mixins and adds the
batch/scale/management overlay (batch + throttle-aware replay, archive/purge
lifecycle, background eviction, disk-durable outbox). This backing is NOT
registered into ``ProviderRegistry.dlq_service`` — the read handlers resolve it
through a handler-layer chain (registry-first, OSS fallback), never the slot.
"""

from __future__ import annotations

import threading

from baldur.services.dlq_capture import DLQCaptureService
from baldur.services.dlq_read.entry_operations import EntryOperationsMixin
from baldur.services.dlq_read.list_operations import ListOperationsMixin
from baldur.services.dlq_read.query_operations import QueryOperationsMixin
from baldur.services.dlq_read.replay_execution import ReplayExecutionMixin

__all__ = [
    "DLQReadService",
    "get_dlq_read_service",
    "reset_dlq_read_service",
]


class DLQReadService(
    ListOperationsMixin,
    QueryOperationsMixin,
    EntryOperationsMixin,
    ReplayExecutionMixin,
    DLQCaptureService,
):
    """OSS DLQ read + single-entry-action backing.

    Lists / inspects captured entries and drives single-entry recovery
    (retry / resolve / force-redrive) over the OSS repository read primitives.
    Construction is I/O-free (config from settings, repository resolved lazily
    on first use), inherited from ``DLQCaptureService``.

    For testing with a mock or in-memory repository::

        service = DLQReadService(repository=repo)
    """


# =============================================================================
# Singleton
# =============================================================================


_read_service: DLQReadService | None = None
_read_service_lock = threading.Lock()


def get_dlq_read_service() -> DLQReadService:
    """Return the process-singleton OSS DLQ read backing."""
    global _read_service
    if _read_service is None:
        with _read_service_lock:
            if _read_service is None:
                _read_service = DLQReadService()
    return _read_service


def reset_dlq_read_service() -> None:
    """Reset the singleton OSS DLQ read backing (test-reset hook)."""
    global _read_service
    _read_service = None
