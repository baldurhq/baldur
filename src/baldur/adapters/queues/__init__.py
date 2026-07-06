"""
Task queue adapters for the baldur system.

This module contains concrete implementations of TaskQueueInterface
and AsyncTaskQueueInterface for different task queue backends.

Available Adapters:
    - CeleryTaskAdapter: Celery-based distributed task queue (sync)
    - SyncTaskAdapter: Synchronous execution for testing (sync)
    - ArqTaskAdapter: arq-based async task queue (async, requires arq)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from baldur.adapters.queues.celery_adapter import (
    CeleryTaskAdapter,
)
from baldur.adapters.queues.sync_adapter import (
    SyncTaskAdapter,
)

if TYPE_CHECKING:
    from baldur.adapters.queues.arq_adapter import ArqTaskAdapter

__all__ = [
    "ArqTaskAdapter",
    "CeleryTaskAdapter",
    "SyncTaskAdapter",
]


def __getattr__(name: str):
    if name == "ArqTaskAdapter":
        from baldur.adapters.queues.arq_adapter import ArqTaskAdapter

        return ArqTaskAdapter
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
