"""
DLQ Outbox — non-blocking RingBuffer-backed outbox for the DLQ store path.

Lifts ``store_to_dlq`` / ``DLQService.store_failure`` off ``protect()``'s
failure hot path so the request thread does not block on the synchronous
DLQ write (impl doc 486).

Public API:
- ``Outbox`` — RingBuffer wrapper + lifecycle owner
- ``DLQOutboxWorker`` — daemon-thread batch drainer
- ``get_outbox()`` — process-singleton accessor
- ``reset_dlq_outbox()`` — drain + stop + clear (used by reset_protect_caches)
- ``flush_and_wait()`` — module-level drain-with-deadline helper
- ``setup_dlq_outbox()`` — eager-start hook called by ``baldur.init()``
  (also wires DAEMON_WORKER_DIED / RESPAWNED EventBus subscribers per
  impl 489 D8)
"""

from __future__ import annotations

from baldur.services.dlq_outbox.outbox import (
    Outbox,
    OutboxStats,
    flush_and_wait,
    get_outbox,
    reset_dlq_outbox,
    setup_dlq_outbox,
)
from baldur.services.dlq_outbox.worker import DLQOutboxWorker

__all__ = [
    "Outbox",
    "OutboxStats",
    "DLQOutboxWorker",
    "get_outbox",
    "reset_dlq_outbox",
    "flush_and_wait",
    "setup_dlq_outbox",
]
