"""
DLQ Outbox Worker — background daemon thread that drains the outbox.

Pattern source: ``baldur/audit/performance/async_writer.py`` ``_writer_loop``
(per-iteration try/except + batch flush). Extends with #486 D11 resilience
guards: per-iteration error containment, ExponentialBackoff on consecutive
``sync_writer`` failures, graceful-shutdown emergency dump via the existing
``_write_to_local_fallback`` no-loss tier.

Cross-shape observability + respawn (impl 489 D9):
- Constructs a ``DaemonWorkerHandle`` and registers it under
  ``"DLQOutboxWorker"`` so the unified ``DaemonWorkerProbe`` picks it up.
- ``_spawn_thread()`` is the per-thread spawn helper that ``restart_callback``
  points at — bypasses the public ``start()`` running-flag guard so the
  respawn coordinator can re-create the dead thread.
- Loop body emits ``handle.heartbeat()`` and ``handle.observe_iteration(d)``
  per iteration so liveness staleness and gradual-slowdown metrics work.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import structlog

from baldur.core.backoff import ExponentialBackoff
from baldur.meta.daemon_worker import DaemonWorkerHandle
from baldur.metrics.recorders.daemon_worker import (
    register_daemon_worker,
    unregister_daemon_worker,
)

if TYPE_CHECKING:
    from baldur.audit.ring_buffer import RingBuffer

logger = structlog.get_logger()

# Threshold of consecutive failed flushes before the worker starts sleeping
# the ExponentialBackoff delay between iterations. Single transient errors
# should not delay the queue.
_FAILURE_BACKOFF_THRESHOLD = 3

# Cap so the slowest exponential step still completes within the join timeout
# of a typical shutdown.
_BACKOFF_BASE_DELAY = 0.1
_BACKOFF_MAX_DELAY = 10.0

_WORKER_NAME = "DLQOutboxWorker"


class DLQOutboxWorker:
    """Daemon-thread drainer for the DLQ outbox RingBuffer.

    Composition with ``Outbox``:
        worker = DLQOutboxWorker(
            buffer=outbox._buffer,
            sync_writer=lambda kwargs: get_dlq_service().store_failure(
                mode="sync", **kwargs
            ),
            batch_size=settings.batch_size,
            flush_interval_seconds=settings.flush_interval_seconds,
            on_emergency_dump=lambda batch: ...,
            on_processing_delay=lambda enqueue_time: ...,
        )
        worker.start()

    The ``sync_writer`` callable is the only mockable surface for unit tests
    (per Testability Notes in 486).
    """

    def __init__(
        self,
        buffer: RingBuffer,
        sync_writer: Callable[[dict[str, Any]], Any],
        batch_size: int = 50,
        flush_interval_seconds: float = 0.1,
        on_emergency_dump: Callable[[list[dict[str, Any]]], None] | None = None,
        on_processing_delay: Callable[[float, str], None] | None = None,
    ) -> None:
        self._buffer = buffer
        self._sync_writer = sync_writer
        self._batch_size = batch_size
        self._flush_interval = flush_interval_seconds
        self._on_emergency_dump = on_emergency_dump
        self._on_processing_delay = on_processing_delay

        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._is_running = False

        # Resilience state (D11.2)
        self._consecutive_failures = 0
        self._backoff = ExponentialBackoff(
            base_delay=_BACKOFF_BASE_DELAY,
            max_delay=_BACKOFF_MAX_DELAY,
            multiplier=2.0,
            jitter=True,
        )

        # Stats
        self._entries_written = 0
        self._entries_failed = 0
        # D6 — entries popped off the buffer but not yet written/failed (the
        # pop->increment window). Worker-thread-owned; read as a single
        # GIL-atomic reference (same pattern as the counters above, so no
        # lock is added per the lock-symmetry single-atomic-read exemption).
        self._in_flight = 0
        # Entries removed from the buffer via the shutdown emergency-dump path
        # (stop() final-timeout): dumped to on_emergency_dump when wired, else
        # dropped after a WARNING. A terminal conservation bucket so the
        # invariant stays closed across shutdown too, not only normal operation.
        self._entries_emergency_dumped = 0

        # Cross-shape observability handle (impl 489 D4 / D9). Constructed
        # lazily on start() so callers can build the worker without
        # touching the daemon_worker settings module.
        self._handle: DaemonWorkerHandle | None = None
        # Track the most recent enqueue→pop delay so the unified
        # processing_delay gauge reflects the worker's pop residency.
        self._last_processing_delay_seconds = 0.0

    @property
    def is_alive(self) -> bool:
        """True when the daemon thread exists and ``Thread.is_alive()``."""
        return self._thread is not None and self._thread.is_alive()

    @property
    def is_running(self) -> bool:
        """True when ``start()`` has been called and ``stop()`` has not."""
        return self._is_running

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    @property
    def entries_written(self) -> int:
        return self._entries_written

    @property
    def entries_failed(self) -> int:
        return self._entries_failed

    @property
    def in_flight(self) -> int:
        """Entries popped from the buffer but not yet written/failed (D6)."""
        return self._in_flight

    @property
    def entries_emergency_dumped(self) -> int:
        """Entries removed via the shutdown emergency-dump path (terminal)."""
        return self._entries_emergency_dumped

    @property
    def handle(self) -> DaemonWorkerHandle | None:
        """Cross-shape observability handle (impl 489)."""
        return self._handle

    def start(self) -> None:
        """Start the daemon thread. Idempotent."""
        if self._is_running:
            return
        self._is_running = True
        self._stop_event.clear()
        self._spawn_thread()
        assert self._thread is not None  # spawn always sets non-None
        self._handle = DaemonWorkerHandle(
            thread=self._thread,
            tick_interval_seconds=self._flush_interval,
            restart_callback=self._spawn_thread,
            processing_delay_provider=lambda: self._last_processing_delay_seconds,
        )
        register_daemon_worker(_WORKER_NAME, self._handle)
        logger.info("dlq_outbox.worker_started")

    def _spawn_thread(self) -> None:
        """Construct + start a fresh writer thread WITHOUT the running guard.

        This is the per-thread helper that the cross-shape respawn
        coordinator calls when the daemon thread has died (impl 489 D9).
        Public ``start()`` early-returns on the running flag, so a respawn
        callback that pointed at ``start()`` would silently no-op.
        """
        # A freshly spawned thread has nothing in flight by definition. Reset so
        # a crash mid-_flush_batch (a BaseException escaping the per-entry
        # finally, e.g. MemoryError) cannot leak a positive in_flight across the
        # cross-shape respawn (impl 489 D9) — which would otherwise make
        # flush_and_wait block to its timeout forever and break the conservation
        # invariant permanently after recovery. Harmless on the initial start().
        self._in_flight = 0
        self._thread = threading.Thread(
            target=self._writer_loop_with_crash_capture,
            daemon=True,
            name=_WORKER_NAME,
        )
        self._thread.start()
        if self._handle is not None:
            # Respawn: rebind the handle's thread reference so the probe
            # observes the new thread on the next tick.
            self._handle.thread = self._thread

    def _writer_loop_with_crash_capture(self) -> None:
        """Wrap _writer_loop so an uncaught exception populates handle.last_crash_reason."""
        try:
            self._writer_loop()
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as e:
            if self._handle is not None:
                self._handle.record_crash(e)
            raise

    def stop(self, timeout: float = 5.0) -> int:
        """Signal stop and wait up to ``timeout`` seconds for drain.

        Returns the count of remaining entries that timed out and were
        emergency-dumped via ``on_emergency_dump`` (D11.3). When
        ``on_emergency_dump`` is None, remaining entries are dropped after a
        WARNING log.
        """
        if not self._is_running:
            return 0

        if self._handle is not None:
            self._handle.is_stopping = True

        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)

        self._is_running = False

        # Unregister BEFORE the join's is_alive check so a leaked OS thread
        # does not show up in subsequent probe ticks.
        unregister_daemon_worker(_WORKER_NAME)

        if self._thread is not None and self._thread.is_alive():
            logger.critical(
                "daemon_worker.stop_join_timeout",
                worker_name=_WORKER_NAME,
                join_timeout_seconds=timeout,
            )

        # Drain any entries still queued at deadline through the emergency
        # path so no data is lost on shutdown timeout.
        remaining = self._buffer.get_all() if self._buffer is not None else []
        if remaining:
            logger.warning(
                "dlq_outbox.shutdown_emergency_dump",
                entries_dumped=len(remaining),
            )
            if self._on_emergency_dump is not None:
                try:
                    self._on_emergency_dump([item[1] for item in remaining])
                except Exception as e:
                    logger.exception(
                        "dlq_outbox.emergency_dump_failed",
                        error=e,
                    )
            # Clear after dump regardless — on_emergency_dump owns persistence.
            self._buffer.clear()
            # Account the dumped entries in the terminal bucket so the
            # conservation invariant stays closed across shutdown (they left
            # ``size`` via the emergency path, not the normal write path).
            self._entries_emergency_dumped += len(remaining)

        logger.info(
            "dlq_outbox.worker_stopped",
            entries_written=self._entries_written,
            entries_failed=self._entries_failed,
            remaining_dumped=len(remaining),
        )
        return len(remaining)

    def _drain_once(self, last_flush: float) -> tuple[float, bool]:
        """One size-check->decide->flush cycle (D1). Returns ``(last_flush, flushed)``.

        The non-destructive ``size`` read drives the ``should_flush`` decision so
        a partial batch that is not yet due stays in the buffer (re-checked next
        iteration) instead of being popped and discarded. An entry leaves the
        buffer only when it is about to be flushed via ``get_batch`` inside the
        flush branch — the zero-loss invariant is structural, not by convention.

        ``size`` is O(1). Reading the entries' contents just to take ``len`` (e.g.
        ``peek_batch``, which copies the whole deque) would scale the decision
        with buffer depth for a value ``size`` already provides — so the contents
        are never materialized here.
        """
        size = self._buffer.size
        should_flush = size > 0 and (
            size >= self._batch_size
            or time.monotonic() - last_flush >= self._flush_interval
        )
        if not should_flush:
            return last_flush, False
        # ``get_batch``'s actual result is the sole source of truth for the
        # flush: a front entry displaced by a DROP_OLDEST eviction between the
        # size read and the pop was an observable backpressure drop (counted in
        # total_dropped) — never a silent loss.
        self._flush_batch(self._buffer.get_batch(max_size=self._batch_size))
        return time.monotonic(), True

    def _writer_loop(self) -> None:  # noqa: C901
        """Background drain loop with per-iteration error containment (D11.1).

        Owns the thread lifecycle, pacing (idle-wait / backoff sleep), and
        observability; the size-check->decide->flush core lives in
        ``_drain_once`` (D3). ``_drain_once`` MUST stay inside the per-iteration
        try/except so a transient ``size``/``get_batch`` raise is contained
        (thread never dies; the still-buffered entry is retried next iteration).
        """
        last_flush = time.monotonic()
        while not self._stop_event.is_set():
            iter_start = time.monotonic()
            try:
                last_flush, flushed = self._drain_once(last_flush)

                if flushed and self._consecutive_failures >= _FAILURE_BACKOFF_THRESHOLD:
                    # D11.2 — backoff sleep prevents busy-loop on extended
                    # downstream outage. ``calculate(attempt)`` is 1-indexed;
                    # pass the failure count directly.
                    delay = self._backoff.calculate(self._consecutive_failures)
                    if self._handle is not None:
                        self._handle.observe_iteration(time.monotonic() - iter_start)
                        self._handle.heartbeat()
                    # Use stop_event.wait so SIGTERM still preempts the sleep.
                    if self._stop_event.wait(timeout=delay):
                        break
                    continue
                if self._handle is not None:
                    self._handle.observe_iteration(time.monotonic() - iter_start)
                    self._handle.heartbeat()
                # Idle — wait briefly so the loop is not a hot poll.
                if not flushed and self._stop_event.wait(timeout=self._flush_interval):
                    break
            except Exception as e:
                # D11.1 — thread MUST never die on a transient error.
                logger.exception("dlq_outbox.writer_loop_error", error=e)
                if self._handle is not None:
                    self._handle.heartbeat()
                # 3b — pace the error path; a persistent _drain_once raise must
                # not hot-spin. SIGTERM still preempts via stop_event.wait.
                if self._stop_event.wait(timeout=self._flush_interval):
                    break

        # Final drain on stop.
        try:
            tail = self._buffer.get_batch(max_size=self._batch_size * 4)
            if tail:
                self._flush_batch(tail)
        except Exception as e:
            logger.exception("dlq_outbox.final_drain_error", error=e)

    def _flush_batch(self, batch: list[tuple[float, dict[str, Any]]]) -> None:
        """Dispatch a batch to ``sync_writer``, recording per-entry outcomes."""
        any_failed = False
        # D6 — the batch is now off the buffer (size dropped) but not yet
        # written/failed. Count it as in-flight until each entry's write
        # resolves so flush_and_wait and the conservation invariant do not
        # undercount the pop->increment window.
        self._in_flight += len(batch)
        for enqueue_time, kwargs in batch:
            domain = str(kwargs.get("domain", "default"))
            try:
                delay = time.monotonic() - enqueue_time
                self._last_processing_delay_seconds = delay
                if self._on_processing_delay is not None:
                    try:
                        self._on_processing_delay(delay, domain)
                    except Exception:
                        pass
                self._sync_writer(kwargs)
                self._entries_written += 1
            except Exception as e:
                self._entries_failed += 1
                any_failed = True
                logger.exception(
                    "dlq_outbox.entry_write_failed",
                    domain=domain,
                    error=e,
                )
            finally:
                # One decrement per entry whether written or failed.
                self._in_flight -= 1

        if any_failed:
            self._consecutive_failures += 1
        else:
            if self._consecutive_failures > 0:
                self._consecutive_failures = 0
                self._backoff.reset()
