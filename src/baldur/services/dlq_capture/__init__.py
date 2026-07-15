"""OSS DLQ capture backing package.

``DLQCaptureService`` is the OSS-tier DLQ capture core — it durably captures
failed operations, dispatches through the async outbox, and falls back to local
disk storage. The PRO ``DLQService`` inherits it and overlays lazy-eviction
overflow, disk-durable outbox, and throttled replay.
"""

from __future__ import annotations

from baldur.services.dlq_capture.overflow import (
    DLQOverflowStrategy,
    OverflowResult,
    enforce_overflow_eviction,
    handle_overflow,
    reset_overflow_state,
)
from baldur.services.dlq_capture.service import (
    DLQCaptureService,
    get_dlq_capture_service,
    reset_dlq_capture_service,
    resolve_dlq_backing,
    resolve_dlq_backing_tier,
)

__all__ = [
    "DLQCaptureService",
    "DLQOverflowStrategy",
    "OverflowResult",
    "enforce_overflow_eviction",
    "get_dlq_capture_service",
    "handle_overflow",
    "reset_dlq_capture_service",
    "reset_overflow_state",
    "resolve_dlq_backing",
    "resolve_dlq_backing_tier",
]
