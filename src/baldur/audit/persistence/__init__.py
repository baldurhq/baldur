"""Disk-Persistent Buffer module.

LMDB-based persistent buffer that survives pod restarts.

Key components:
- DiskBufferSettings: Buffer configuration
- DiskPersistentBuffer: LMDB-based persistent buffer
- DiskBufferAdapter: InMemoryAuditBuffer-compatible adapter
- DiskSpaceMonitor: Ratio-based disk space monitoring
- GroupCommitWriter: Batched I/O optimisation
- DeadLetterStore: Poison-pill entry isolation
- MmapBuffer: mmap-based alternative buffer (stdlib only)
- drain_on_startup: Event recovery on pod restart

Usage::

    from baldur.audit.persistence import (
        DiskBufferSettings,
        DiskPersistentBuffer,
        get_disk_buffer,
    )

    # Create buffer
    buffer = DiskPersistentBuffer()

    # Store event
    buffer.put({"event_type": "audit", "data": "..."})

    # Read
    for entry in buffer.iter_entries():
        print(entry.data)

    # Flush
    buffer.flush_to(lambda entries: send_to_kafka(entries))
"""

from __future__ import annotations

from baldur.audit.persistence.config import (
    DiskBufferSettings,
    get_disk_buffer_settings,
    reset_disk_buffer_settings,
)
from baldur.audit.persistence.dead_letter_store import DeadLetterStore
from baldur.audit.persistence.disk_buffer import (
    DiskPersistentBuffer,
    get_disk_buffer,
    reset_disk_buffer,
)
from baldur.audit.persistence.disk_buffer_adapter import DiskBufferAdapter
from baldur.audit.persistence.disk_buffer_models import (
    BufferEntry,
    BufferState,
    DiskBufferError,
)
from baldur.audit.persistence.disk_space_monitor import DiskSpaceMonitor
from baldur.audit.persistence.group_commit import GroupCommitWriter
from baldur.audit.persistence.migration import (
    DrainResult,
    async_drain_on_startup,
    drain_on_startup,
)

__all__ = [
    # Config
    "DiskBufferSettings",
    "get_disk_buffer_settings",
    "reset_disk_buffer_settings",
    # Models / Exceptions
    "BufferEntry",
    "BufferState",
    "DiskBufferError",
    # Disk Buffer
    "DiskBufferAdapter",
    "DiskPersistentBuffer",
    "get_disk_buffer",
    "reset_disk_buffer",
    # Sub-components
    "DeadLetterStore",
    "DiskSpaceMonitor",
    "GroupCommitWriter",
    # Migration
    "DrainResult",
    "async_drain_on_startup",
    "drain_on_startup",
]
