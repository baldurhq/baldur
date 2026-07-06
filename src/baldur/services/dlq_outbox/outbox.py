"""
DLQ Outbox — RingBuffer producer + worker lifecycle owner.

Producer hot path: ``Outbox.put(kwargs)`` — wraps as ``(enqueue_time, kwargs)``
and calls ``RingBuffer.put`` (lock-bounded ~50-100 ns). The ``enqueue_time``
is used by the worker to observe ``dlq_outbox_processing_delay_seconds``
when popping the entry (D4 leading-indicator).

Drop policy: DROP_OLDEST. The drop-rate threshold callback emits
``dlq.outbox_drop_threshold_breached`` log + Prometheus counter +
``DLQ_OUTBOX_DROP_THRESHOLD_BREACHED`` EventBus event so operators see drops
before they translate into customer-visible loss.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import structlog

from baldur.audit.ring_buffer import RingBuffer, RingBufferStats
from baldur.services.dlq_outbox.worker import DLQOutboxWorker
from baldur.settings.backpressure import BackpressureStrategy

if TYPE_CHECKING:
    pass

logger = structlog.get_logger()


@dataclass
class OutboxStats:
    """Snapshot of outbox + worker state."""

    capacity: int
    size: int
    total_enqueued: int
    total_dropped: int
    drop_rate: float
    entries_written: int
    entries_failed: int
    consecutive_failures: int
    worker_alive: bool
    worker_dead_coercions: int
    # D6 — entries popped from the buffer but not yet written/failed.
    in_flight: int
    # Entries removed via the shutdown emergency-dump path (stop() timeout).
    # Together with in_flight this closes the conservation invariant
    # continuously — across normal operation AND shutdown — so a monitor never
    # sees a phantom shortfall after a graceful-shutdown dump:
    # total_enqueued == entries_written + entries_failed + total_dropped
    #                   + size + in_flight + entries_emergency_dumped
    entries_emergency_dumped: int


# Module-level singleton state. The lifecycle is owned by ``baldur.init()``
# (D7) and by ``reset_dlq_outbox`` for test isolation (D8).
_outbox: Outbox | None = None
_outbox_lock = threading.Lock()

# Producer-side fail-open flag. Toggled by EventBus subscribers wired in
# ``setup_dlq_outbox()`` (impl 489 D8): the cross-shape ``DaemonWorkerProbe``
# emits ``DAEMON_WORKER_DIED`` on dead-thread detection (sets True) and
# ``DAEMON_WORKER_RESPAWNED`` on successful auto-restart (sets False).
_worker_dead: bool = False
_worker_dead_lock = threading.Lock()
_worker_dead_coercions: int = 0
_DLQ_OUTBOX_WORKER_NAME = "DLQOutboxWorker"


class Outbox:
    """RingBuffer-backed DLQ outbox.

    Constructor takes a pre-built ``RingBuffer`` and ``DLQOutboxWorker`` so
    tests can inject mocks (per Testability Notes in 486). Production path
    is ``Outbox.from_settings()``.
    """

    def __init__(
        self,
        buffer: RingBuffer,
        worker: DLQOutboxWorker,
    ) -> None:
        self._buffer = buffer
        self._worker = worker

    @classmethod
    def from_settings(
        cls,
        sync_writer: Callable[[dict[str, Any]], Any] | None = None,
        emergency_dump: Callable[[list[dict[str, Any]]], None] | None = None,
    ) -> Outbox:
        """Build an Outbox from ``DLQOutboxSettings`` with default wiring.

        ``sync_writer`` defaults to ``DLQService.store_failure(mode="sync", ...)``
        via lazy import. ``emergency_dump`` defaults to dispatching each
        kwargs through ``DLQService._write_to_local_fallback`` (D11.3).
        """
        from baldur.settings.dlq_outbox import get_dlq_outbox_settings

        settings = get_dlq_outbox_settings()

        buffer: RingBuffer = RingBuffer(
            capacity=settings.capacity,
            strategy=BackpressureStrategy.DROP_OLDEST,
            drop_rate_threshold=settings.drop_rate_threshold,
            on_drop_threshold=_on_drop_threshold,
        )

        if sync_writer is None:
            sync_writer = _default_sync_writer
        if emergency_dump is None:
            emergency_dump = _default_emergency_dump

        worker = DLQOutboxWorker(
            buffer=buffer,
            sync_writer=sync_writer,
            batch_size=settings.batch_size,
            flush_interval_seconds=settings.flush_interval_seconds,
            on_emergency_dump=emergency_dump,
            on_processing_delay=_on_processing_delay,
        )
        return cls(buffer=buffer, worker=worker)

    # ------------------------------------------------------------------
    # Producer surface
    # ------------------------------------------------------------------

    def put(self, kwargs: dict[str, Any]) -> bool:
        """Enqueue ``kwargs`` for async dispatch.

        Returns True if enqueued (or dropped-oldest), False only when the
        underlying RingBuffer is configured with DROP_NEWEST and is full.
        """
        # D4 — wrap with enqueue_time so the worker can observe processing
        # delay when popping the entry.
        return self._buffer.put((time.monotonic(), kwargs))

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        self._worker.start()

    def stop(self, timeout: float = 5.0) -> int:
        return self._worker.stop(timeout=timeout)

    def flush_and_wait(self, timeout: float = 5.0) -> int:
        """Drain queued entries through the worker, blocking up to ``timeout``.

        Returns the count of entries successfully drained. Pending entries
        beyond the deadline are emergency-dumped (D11.3).
        """
        deadline = time.monotonic() + timeout
        drained_before = self._worker.entries_written
        # D6 — block until the buffer is empty AND no entry is mid-write. The
        # worker pops a batch (buffer size drops) before each per-entry write
        # resolves, so gating on size alone would read entries_written before
        # the increment and undercount. ``in_flight`` closes that
        # pop->increment window so the returned delta is settled.
        while time.monotonic() < deadline and (
            self._buffer.size > 0 or self._worker.in_flight > 0
        ):
            time.sleep(0.01)
        drained_after = self._worker.entries_written
        return drained_after - drained_before

    # ------------------------------------------------------------------
    # Stats / introspection
    # ------------------------------------------------------------------

    def get_stats(self) -> OutboxStats:
        bs: RingBufferStats = self._buffer.get_stats()
        return OutboxStats(
            capacity=bs.capacity,
            size=bs.size,
            total_enqueued=bs.total_enqueued,
            total_dropped=bs.total_dropped,
            drop_rate=bs.drop_rate,
            entries_written=self._worker.entries_written,
            entries_failed=self._worker.entries_failed,
            consecutive_failures=self._worker.consecutive_failures,
            worker_alive=self._worker.is_alive,
            worker_dead_coercions=_worker_dead_coercions,
            in_flight=self._worker.in_flight,
            entries_emergency_dumped=self._worker.entries_emergency_dumped,
        )

    @property
    def buffer(self) -> RingBuffer:
        return self._buffer

    @property
    def worker(self) -> DLQOutboxWorker:
        return self._worker


# =============================================================================
# Module-level helpers
# =============================================================================


def get_outbox() -> Outbox:
    """Return the process-singleton outbox, building lazily on first call.

    The eager-start path runs through ``setup_dlq_outbox`` from
    ``baldur.init()``; this lazy path covers tests / scripts that touch the
    DLQ store before init().
    """
    global _outbox
    if _outbox is not None:
        return _outbox
    with _outbox_lock:
        if _outbox is not None:
            return _outbox
        _outbox = Outbox.from_settings()
        _outbox.start()
        return _outbox


def setup_dlq_outbox() -> bool:
    """Eager-start hook called from ``baldur.init()`` (D7).

    Idempotent. Returns True on first start, False on re-entry.

    Also wires the two ``DAEMON_WORKER_*`` EventBus subscribers (impl 489
    D8): when the cross-shape ``DaemonWorkerProbe`` reports the
    ``DLQOutboxWorker`` daemon thread as dead, ``_worker_dead`` flips True
    and producer-side ``Outbox.put`` calls coerce to the sync writer
    (preserves D11.4 fail-open). On a successful auto-respawn,
    ``_worker_dead`` flips back False so the async fast path resumes.
    """
    global _outbox
    with _outbox_lock:
        if _outbox is not None:
            return False
        _outbox = Outbox.from_settings()
        _outbox.start()
        _wire_worker_lifecycle_subscribers()
        logger.info("dlq_outbox.setup_completed")
        return True


def _wire_worker_lifecycle_subscribers() -> None:
    """Subscribe the DAEMON_WORKER_DIED / RESPAWNED handlers (impl 489 D8)."""
    try:
        from baldur.services.event_bus.bus.convenience import get_event_bus
        from baldur.services.event_bus.bus.event_types import EventType

        bus = get_event_bus()
        bus.subscribe(EventType.DAEMON_WORKER_DIED, _on_daemon_worker_died)
        bus.subscribe(EventType.DAEMON_WORKER_RESPAWNED, _on_daemon_worker_respawned)
    except Exception as e:
        logger.warning("dlq_outbox.subscribe_worker_lifecycle_failed", error=e)


def _on_daemon_worker_died(event: Any) -> None:
    """Set the producer fail-open flag when the DLQOutboxWorker dies."""
    global _worker_dead
    data = getattr(event, "data", None) or {}
    if data.get("worker_name") != _DLQ_OUTBOX_WORKER_NAME:
        return
    with _worker_dead_lock:
        _worker_dead = True


def _on_daemon_worker_respawned(event: Any) -> None:
    """Clear the producer fail-open flag on successful DLQOutboxWorker respawn."""
    global _worker_dead
    data = getattr(event, "data", None) or {}
    if data.get("worker_name") != _DLQ_OUTBOX_WORKER_NAME:
        return
    with _worker_dead_lock:
        _worker_dead = False


def reset_dlq_outbox() -> int:
    """Drain pending entries, stop the worker, clear state.

    Wired into ``baldur.protect_facade.reset_protect_caches`` (D8). MUST drain
    rather than just clear so queued entries from the prior test do not
    surface in the next test's worker.
    """
    global _outbox, _worker_dead, _worker_dead_coercions
    with _outbox_lock:
        if _outbox is None:
            with _worker_dead_lock:
                _worker_dead = False
                _worker_dead_coercions = 0
            return 0
        # Best-effort: give the worker a short window to drain before stop.
        try:
            _outbox.flush_and_wait(timeout=1.0)
        except Exception:
            pass
        remaining = _outbox.stop(timeout=1.0)
        _outbox = None

    with _worker_dead_lock:
        _worker_dead = False
        _worker_dead_coercions = 0
    return remaining


def flush_and_wait(timeout: float = 5.0) -> int:
    """Module-level shortcut for ``get_outbox().flush_and_wait(timeout)``."""
    if _outbox is None:
        return 0
    return _outbox.flush_and_wait(timeout=timeout)


# =============================================================================
# Producer-side fail-open accessors (impl 489 D8 — flag toggled by EventBus
# subscribers wired in setup_dlq_outbox)
# =============================================================================


def is_worker_dead() -> bool:
    """Producer-side fail-open check used by ``store_to_dlq`` async dispatch."""
    return _worker_dead


def record_worker_dead_coercion() -> None:
    """Increment the producer-side coercion counter (called by the dispatch
    path when ``is_worker_dead()`` forces a sync coercion).
    """
    global _worker_dead_coercions
    with _worker_dead_lock:
        _worker_dead_coercions += 1
    try:
        from baldur.services.metrics.definitions import (
            dlq_outbox_worker_dead_coercions_total,
        )

        dlq_outbox_worker_dead_coercions_total.inc()
    except Exception:
        pass


# =============================================================================
# Default wiring helpers
# =============================================================================


def _default_sync_writer(kwargs: dict[str, Any]) -> Any:
    """Lazy-resolves ``DLQService`` and dispatches the kwargs synchronously.

    Lives in the worker thread, so the PRO import cost moves entirely off
    the producer hot path (D1).
    """
    from baldur.factory.registry import ProviderRegistry

    service = ProviderRegistry.dlq_service.safe_get()
    if service is None:
        raise RuntimeError("DLQ outbox requires baldur_pro DLQService")
    return service.store_failure(mode="sync", **kwargs)


def _default_emergency_dump(batch: list[dict[str, Any]]) -> None:
    """Dispatch each remaining entry through ``DLQService._write_to_local_fallback``.

    Reuses the existing zero-loss disk fallback (D11.3) — no new dump
    format introduced. Called only on shutdown timeout when the worker
    cannot drain in time.
    """
    try:
        from baldur.factory.registry import ProviderRegistry

        service = ProviderRegistry.dlq_service.safe_get()
        if service is None:
            raise RuntimeError("baldur_pro DLQService not registered")
    except Exception as e:
        logger.warning("dlq_outbox.emergency_dump_unavailable", error=e)
        return

    # The OSS DLQService Protocol intentionally omits `_write_to_local_fallback`
    # because the disk-fallback path is a PRO impl detail (D11.3). We reach
    # through to it here only on shutdown emergency dump; getattr keeps the
    # OSS Protocol contract tight while preserving the zero-loss invariant.
    fallback = getattr(service, "_write_to_local_fallback", None)
    if fallback is None:
        logger.warning(
            "dlq_outbox.emergency_dump_unsupported",
            reason="DLQService does not expose _write_to_local_fallback",
        )
        return

    for kwargs in batch:
        try:
            fallback(kwargs, "shutdown_emergency_dump")
        except Exception as e:
            logger.exception("dlq_outbox.emergency_dump_entry_failed", error=e)


# =============================================================================
# Drop-rate alert callback (D4)
# =============================================================================


def _on_drop_threshold(stats: RingBufferStats) -> None:
    """RingBuffer drop-rate threshold callback.

    1. WARNING log
    2. Prometheus counter increment
    3. DLQ_OUTBOX_DROP_THRESHOLD_BREACHED EventBus event
    """
    logger.warning(
        "dlq.outbox_drop_threshold_breached",
        capacity=stats.capacity,
        size=stats.size,
        total_dropped=stats.total_dropped,
        drop_rate=stats.drop_rate,
    )
    try:
        from baldur.services.metrics.definitions import dlq_outbox_drops_total

        dlq_outbox_drops_total.labels(domain="default").inc()
    except Exception:
        pass

    try:
        from baldur.services.event_bus.bus.convenience import get_event_bus
        from baldur.services.event_bus.bus.event_types import (
            EventPriority,
            EventType,
        )

        bus = get_event_bus()
        bus.emit(
            EventType.DLQ_OUTBOX_DROP_THRESHOLD_BREACHED,
            data={
                "capacity": stats.capacity,
                "size": stats.size,
                "total_dropped": stats.total_dropped,
                "drop_rate": stats.drop_rate,
            },
            source="dlq_outbox",
            priority=EventPriority.HIGH,
        )
    except Exception as e:
        logger.debug("dlq_outbox.drop_event_emit_failed", error=e)


def _on_processing_delay(delay_seconds: float, domain: str) -> None:
    """Worker-side enqueue→pop delay observation (D4 leading indicator)."""
    try:
        from baldur.services.metrics.definitions import (
            dlq_outbox_processing_delay_seconds,
        )

        dlq_outbox_processing_delay_seconds.labels(domain=domain).observe(delay_seconds)
    except Exception:
        pass
