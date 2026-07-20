"""
Disk Buffer drain-on-startup migration.

Flushes previously persisted events to primary storage when a pod restarts.
Call at application startup to recover unprocessed events from the previous
session.

Usage:
    from baldur.audit.persistence import (
        DiskPersistentBuffer,
        drain_on_startup,
    )

    buffer = DiskPersistentBuffer()

    def send_to_primary(entries: list[dict]) -> bool:
        # Send to primary storage (Kafka, DB, etc.)
        return True

    result = drain_on_startup(
        buffer=buffer,
        flush_handler=send_to_primary,
    )

    print(f"Drained: {result.drained}, Failed: {result.failed}")
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from baldur.audit.persistence.disk_buffer import (
        DiskPersistentBuffer,
    )

logger = structlog.get_logger()


@dataclass
class DrainResult:
    """Result of a drain operation."""

    drained: int = 0
    """Number of entries flushed successfully."""

    failed: int = 0
    """Number of entries that failed to flush."""

    skipped: int = 0
    """Number of entries skipped."""

    duration_seconds: float = 0.0
    """Elapsed time of the operation (seconds)."""

    errors: list[str] = field(default_factory=list)
    """List of error messages raised."""


def drain_on_startup(
    buffer: DiskPersistentBuffer,
    flush_handler: Callable[[list[dict[str, Any]]], bool],
    batch_size: int = 100,
    max_batches: int | None = None,
    fail_fast: bool = False,
) -> DrainResult:
    """
    Flush events left in the buffer to primary storage at startup.

    Sends events persisted by the previous session — after a pod restart —
    to primary storage (Kafka, DB, etc.).

    Args:
        buffer: DiskPersistentBuffer instance
        flush_handler: Event batch handler (returns True on success)
        batch_size: Batch size
        max_batches: Maximum number of batches to process (None=unlimited)
        fail_fast: Stop immediately on failure

    Returns:
        DrainResult

    Usage:
        result = drain_on_startup(
            buffer=buffer,
            flush_handler=lambda entries: kafka_producer.send_batch(entries),
            batch_size=100,
        )
    """
    start_time = time.time()
    result = DrainResult(errors=[])

    entry_count = buffer.count()
    if entry_count == 0:
        logger.info("drain_on_startup.no_pending_entries_drain")
        return result

    logger.info(
        "drain_on_startup.draining_pending_entries",
        entry_count=entry_count,
    )

    batches_processed = 0

    while True:
        if max_batches and batches_processed >= max_batches:
            logger.warning(
                "drain_on_startup.max_batches_reached",
                max_batches=max_batches,
            )
            break

        # Fetch a batch
        entries = list(buffer.iter_entries(limit=batch_size))
        if not entries:
            break

        # Invoke the handler
        try:
            # BufferEntry → dict conversion
            entry_dicts = [e.data for e in entries]
            success = flush_handler(entry_dicts)
        except Exception as e:
            error_msg = f"Handler error: {e}"
            logger.exception(
                "drain_on_startup.event",
                error_msg=error_msg,
            )
            result.errors.append(error_msg)
            result.failed += len(entries)

            if fail_fast:
                break
            continue

        if success:
            # Delete on success
            keys = [e.key for e in entries]
            deleted = buffer.delete_batch(keys)
            result.drained += deleted
            logger.debug(
                "drain_on_startup.drained_batch_entries",
                deleted=deleted,
            )
        else:
            # Skip this batch on failure (try the next batch)
            result.skipped += len(entries)
            logger.warning(
                "drain_on_startup.batch_failed_skipping_entries",
                entries_count=len(entries),
            )

            if fail_fast:
                break

        batches_processed += 1

    result.duration_seconds = time.time() - start_time

    logger.info(
        "drain_on_startup.complete",
        drained_count=result.drained,
        failed=result.failed,
        skipped=result.skipped,
        duration_seconds=result.duration_seconds,
    )

    return result


async def async_drain_on_startup(
    buffer: DiskPersistentBuffer,
    async_flush_handler: Callable[[list[dict[str, Any]]], Any],
    batch_size: int = 100,
    max_batches: int | None = None,
) -> DrainResult:
    """
    Async version of drain_on_startup.

    Suitable when the primary-storage handler is asynchronous.

    Args:
        buffer: DiskPersistentBuffer instance
        async_flush_handler: Async event handler
        batch_size: Batch size
        max_batches: Maximum number of batches

    Returns:
        DrainResult

    Usage:
        async def send_to_kafka(entries):
            await kafka_producer.send_batch(entries)
            return True

        result = await async_drain_on_startup(
            buffer=buffer,
            async_flush_handler=send_to_kafka,
        )
    """
    import asyncio

    start_time = time.time()
    result = DrainResult(errors=[])

    entry_count = buffer.count()
    if entry_count == 0:
        return result

    logger.info(
        "async_drain_on_startup.draining_entries",
        entry_count=entry_count,
    )

    batches_processed = 0

    while True:
        if max_batches and batches_processed >= max_batches:
            break

        entries = list(buffer.iter_entries(limit=batch_size))
        if not entries:
            break

        try:
            entry_dicts = [e.data for e in entries]
            success = await async_flush_handler(entry_dicts)
        except Exception as e:
            result.errors.append(str(e))
            result.failed += len(entries)
            continue

        if success:
            keys = [e.key for e in entries]
            deleted = buffer.delete_batch(keys)
            result.drained += deleted
        else:
            result.skipped += len(entries)

        batches_processed += 1
        await asyncio.sleep(0)  # Yield to the event loop

    result.duration_seconds = time.time() - start_time

    logger.info(
        "async_drain_on_startup.complete",
        drained_count=result.drained,
        failed=result.failed,
        skipped=result.skipped,
    )

    return result
