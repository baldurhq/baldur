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
  respawn coordinator can re-create the dead thread. It is the single spawn
  seam: a lock plus a thread-aliveness guard there is what keeps the respawn
  coordinator and ``repair_after_fork()`` from starting one drainer each.
- Loop body emits ``handle.heartbeat()`` and ``handle.observe_iteration(d)``
  per iteration so liveness staleness and gradual-slowdown metrics work.

Fork safety: the writer is a daemon thread, so a process forked from one that
already started it inherits ``_is_running=True`` and a thread that does not run
there. ``repair_after_fork()`` re-owns that state and respawns; the outbox
module reaches it from its own entry-point repair.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
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


@dataclass(frozen=True)
class DropWindow:
    """Ring-buffer drop accounting for ONE worker cycle.

    ``dropped`` / ``enqueued`` / ``drop_rate`` describe the window since the
    previous cycle's read; ``total_dropped`` is the buffer's lifetime count.
    The windowed rate is what an alert must be judged on: a lifetime rate
    dilutes a late drop episode against every enqueue the process ever made,
    so a long-lived process silences exactly the bursts an operator needs.
    """

    dropped: int
    enqueued: int
    drop_rate: float
    capacity: int
    size: int
    total_dropped: int


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
            on_drops_observed=lambda count: ...,
            on_drop_alert=lambda window: ...,
        )
        worker.start()

    The ``sync_writer`` callable is the only mockable surface for unit tests
    (per Testability Notes in 486).

    The worker also owns ring-drop accounting: each cycle it reads the buffer's
    counters and reports the window's drops, so the producer threads never pay
    for alerting the backpressure they hit (779 D12).
    """

    def __init__(
        self,
        buffer: RingBuffer,
        sync_writer: Callable[[dict[str, Any]], Any],
        batch_size: int = 50,
        flush_interval_seconds: float = 0.1,
        on_emergency_dump: Callable[[list[dict[str, Any]]], None] | None = None,
        on_processing_delay: Callable[[float, str], None] | None = None,
        on_drops_observed: Callable[[int], None] | None = None,
        on_drop_alert: Callable[[DropWindow], None] | None = None,
        drop_rate_threshold: float = 0.01,
    ) -> None:
        self._buffer = buffer
        self._sync_writer = sync_writer
        self._batch_size = batch_size
        self._flush_interval = flush_interval_seconds
        self._on_emergency_dump = on_emergency_dump
        self._on_processing_delay = on_processing_delay
        self._on_drops_observed = on_drops_observed
        self._on_drop_alert = on_drop_alert
        self._drop_rate_threshold = drop_rate_threshold

        # Watermarks for the per-cycle drop window. Drop accounting lives on
        # this thread rather than in the buffer's own put(): that callback runs
        # under the ring lock on whichever request thread happened to overflow
        # it, and a WARNING + metric + awaited bus.emit there would block
        # rejecting threads for as long as the slowest event handler takes.
        self._last_seen_enqueued = 0
        self._last_seen_dropped = 0

        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._is_running = False
        # Mutual exclusion between the two spawn paths — the fork-repair
        # sequence and the ``DaemonWorkerProbe`` respawn coordinator, which
        # reaches ``_spawn_thread`` through the handle's ``restart_callback``.
        # Without it both can observe the dead inherited thread and start one
        # writer each, and two drainers on one buffer write every entry twice.
        self._spawn_lock = threading.Lock()

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

        Guarded on the thread object rather than on ``_is_running``: the
        respawn-callback contract forbids consulting the running flag (a fork
        child inherits it True with a thread Python already marked stopped),
        while an aliveness guard still lets a genuinely dead writer respawn and
        stops a second live one. The guard and the ``handle.thread`` rebind sit
        inside one lock so the fork-repair path and a probe tick cannot both
        pass the check in the window between ``Thread.start()`` and the rebind.
        """
        with self._spawn_lock:
            existing = self._thread
            if existing is not None and existing.is_alive():
                logger.debug("dlq_outbox.spawn_skipped_writer_alive")
                return

            # A freshly spawned thread has nothing in flight by definition. Reset
            # so a crash mid-_flush_batch (a BaseException escaping the per-entry
            # finally, e.g. MemoryError) cannot leak a positive in_flight across
            # the cross-shape respawn (impl 489 D9) — which would otherwise make
            # flush_and_wait block to its timeout forever and break the
            # conservation invariant permanently after recovery. Harmless on the
            # initial start().
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

    def repair_after_fork(self) -> None:
        """Re-own the writer state this process inherited across ``fork()``.

        The inherited thread does not run here — ``fork()`` copies only the
        calling thread — so without this the child holds ``_is_running=True``
        with a dead writer, ``start()`` no-ops on that flag, and every entry
        the child enqueues sits in a buffer nothing drains while
        ``store_failure`` reports success.

        Order is pinned: every lock and ``Event`` is renewed before anything
        acquires one (each may have been inherited held by a thread that does
        not exist here), then the statistics restart so this process publishes
        its own, then the handle drops the parent's observations while keeping
        its identity — the registry entry and the inherited ``restart_callback``
        already point at this object, so a probe tick that races the respawn
        below converges on it instead of chasing a stale entry.

        The buffer is repaired by the caller, which owns it; this method
        assumes the entries it drains are this process's own.

        A writer that is already alive here is kept, not replaced. The
        ``DaemonWorkerProbe`` reaches ``_spawn_thread`` through the inherited
        handle's ``restart_callback``, and a tick that lands before this
        repair respawns the dead writer through the *old* spawn seam. The
        repair-first starter order makes that sequence unreachable on the
        ``start_background_workers()`` path, so this branch is defense in
        depth — but nulling the reference to a live writer and spawning a
        second one puts two drainers on one buffer, writing every entry
        twice, with ``stop()`` joining only the newer thread.
        """
        live_thread = self._thread
        if live_thread is not None and not live_thread.is_alive():
            live_thread = None

        self._spawn_lock = threading.Lock()
        self._stop_event = threading.Event()
        if live_thread is None:
            self._thread = None

        # The parent's counts describe the parent's writes. Kept, they would
        # leave the conservation invariant (total_enqueued == written + failed
        # + dropped + size + in_flight + dumped) open in the child, whose
        # buffer counters restart at zero with its contents.
        self._entries_written = 0
        self._entries_failed = 0
        self._entries_emergency_dumped = 0
        self._in_flight = 0
        self._consecutive_failures = 0
        self._backoff.reset()

        # The buffer's counters restart with its contents in the child, so the
        # watermarks must restart too — otherwise the first window's delta is
        # negative and this process's drops stay invisible until it catches up.
        self._last_seen_enqueued = 0
        self._last_seen_dropped = 0

        if self._handle is not None:
            self._handle.reset_after_fork()

        # ``_is_running`` stays True: this worker is running in this process the
        # moment the spawn below returns, and stop() must remain reachable.
        self._is_running = True
        if live_thread is not None:
            # The probe's writer is this process's own live thread; adopt it.
            if self._handle is not None:
                self._handle.thread = live_thread
            logger.info("dlq_outbox.worker_adopted_probe_respawn")
        else:
            self._spawn_thread()
            logger.info("dlq_outbox.worker_respawned_after_fork")

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

    def _observe_drop_window(self, *, alert: bool = True) -> None:
        """Account this cycle's ring drops and evaluate the windowed drop rate.

        ``get_stats()`` is a lock-scoped counter read, so the producer threads
        pay nothing for the accounting and the alert work — a WARNING log, a
        counter increment and an awaited event emit — runs here instead of
        under the ring lock inside ``put``.

        The counters are cumulative, so a cycle delayed by a slow batch store
        reports the full delta on its next pass: drop visibility can lag by a
        cycle, never vanish. That "next pass" has to exist even when there is
        no next iteration, which is why the loop's exit runs one final
        observation — entries dropped while the last ``wait`` was in flight
        would otherwise be dropped from the accounting too. The rate is
        evaluated only when the window enqueued something — a drop happens only
        inside ``put``, which counts the enqueue first, so a zero-enqueue
        window has zero drops and there is no 0/0 to divide.

        Args:
            alert: Whether a breached window may run the alert callback. The
                shutdown observation passes False: its counter update is what
                makes the loss visible afterwards, while its alert would emit
                onto an event bus that is itself being torn down, on the thread
                ``stop()`` is waiting to join.
        """
        stats = self._buffer.get_stats()
        dropped = max(0, stats.total_dropped - self._last_seen_dropped)
        enqueued = max(0, stats.total_enqueued - self._last_seen_enqueued)
        self._last_seen_dropped = stats.total_dropped
        self._last_seen_enqueued = stats.total_enqueued

        if dropped and self._on_drops_observed is not None:
            try:
                self._on_drops_observed(dropped)
            except Exception:
                pass  # An accounting failure must never disrupt the drain

        if not alert or not enqueued or self._on_drop_alert is None:
            return
        drop_rate = dropped / enqueued
        if drop_rate <= self._drop_rate_threshold:
            return
        try:
            self._on_drop_alert(
                DropWindow(
                    dropped=dropped,
                    enqueued=enqueued,
                    drop_rate=drop_rate,
                    capacity=stats.capacity,
                    size=stats.size,
                    total_dropped=stats.total_dropped,
                )
            )
        except Exception:
            pass  # An alert failure must never disrupt the drain

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
                self._observe_drop_window()

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

        # Close the last window. Everything dropped since the loop's final
        # observation happened while it was waiting to be told to stop, and
        # there is no further iteration to report it — without this the drops
        # that a shutdown-under-load produces never reach the counter at all.
        self._observe_drop_window(alert=False)

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
