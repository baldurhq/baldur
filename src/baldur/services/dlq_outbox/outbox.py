"""
DLQ Outbox — RingBuffer producer + worker lifecycle owner.

Producer hot path: ``Outbox.put(kwargs)`` — wraps as ``(enqueue_time, kwargs)``
and calls ``RingBuffer.put`` (lock-bounded ~50-100 ns). The ``enqueue_time``
is used by the worker to observe ``dlq_outbox_processing_delay_seconds``
when popping the entry (D4 leading-indicator).

Drop policy: DROP_OLDEST. Drops are accounted on the worker thread, one window
per drain cycle (779 D12): every dropped entry is counted into
``dlq_outbox_drops_total``, and a window whose drop rate exceeds the threshold
emits ``dlq.outbox_drop_threshold_breached`` + the
``DLQ_OUTBOX_DROP_THRESHOLD_BREACHED`` EventBus event, so operators see drops
before they translate into customer-visible loss. Windowed and worker-side by
design: a lifetime rate silences a late burst in a long-lived process, and the
buffer's own callback would run that alert under the ring lock on a request
thread.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import structlog

from baldur.audit.ring_buffer import RingBuffer, RingBufferStats
from baldur.core.process_utils import fork_repaired
from baldur.services.dlq_outbox.worker import DLQOutboxWorker, DropWindow
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
    # Entries the DLQ store rejected but the local fallback preserved. Neither
    # written nor lost: ``store_failure`` absorbs the repository error and
    # returns, so without this bucket a store outage reads as a clean write.
    entries_soft_failed: int
    entries_failed: int
    consecutive_failures: int
    worker_alive: bool
    worker_dead_coercions: int
    # D6 — entries popped from the buffer but not yet written/failed.
    in_flight: int
    # Entries the shutdown emergency dump reported having written (stop()
    # timeout). Together with in_flight this keeps the conservation relation
    # closed across normal operation AND shutdown, so a monitor never sees a
    # phantom shortfall after a graceful-shutdown dump. The relation is a
    # BOUND, not an equality:
    #   entries_written + entries_soft_failed + entries_failed + total_dropped
    #       + size + in_flight + entries_emergency_dumped
    #       == total_enqueued + <duplicated>
    # The shutdown rescue is deliberately at-least-once — the entry the writer
    # is attempting when the teardown lands is handed to the dump as well as
    # written — so an entry can be enqueued once and counted twice. A strict
    # equality is false by construction on exactly the path that matters.
    entries_emergency_dumped: int


@dataclass(frozen=True)
# verified-by: test_conservation_reports_a_dumped_and_written_entry_as_duplicated
class OutboxShutdownResult:
    """Terminal report of one ``stop_outbox_for_shutdown()`` call.

    Every entry the outbox still owned when the teardown began ends up in
    exactly one bucket, except where the rescue deliberately double-counts:

        dispatched + soft_failed + failed + emergency_dumped + residual
            == pending_at_entry + duplicated

    ``duplicated`` is the overlap the design puts there on purpose. The dump is
    at-least-once: the entry the writer is attempting when the teardown lands is
    handed to the dump as well, so a write that then succeeds is counted twice.
    Reporting the overlap is what keeps ``residual`` from going negative — and a
    negative or unexplained residual is precisely the defect this bucket exists
    to prevent.
    """

    #: ``size + in_flight`` at teardown entry. An entry the writer had already
    #: popped is pending exactly as much as one still on the ring.
    pending_at_entry: int
    #: Handed to the DLQ store without raising. NOT "stored": the store path
    #: absorbs repository errors and returns, which is what the two buckets
    #: below separate out.
    dispatched: int
    #: Store write failed, local fallback preserved the entry. Degraded, kept.
    soft_failed: int
    #: Reached no store at all — a hard raise, or a local fallback that failed
    #: too. This bucket means lost.
    failed: int
    #: Written by the shutdown emergency dump.
    emergency_dumped: int
    #: Handed to the dump and not written by it: a blown dump deadline, a
    #: raising callback, an unresolvable backing. These die with the process.
    #: The dump's own count, never a subtraction of the buckets above.
    residual: int
    #: Entries counted in two buckets by design (see the class docstring).
    duplicated: int


# Module-level singleton state. The lifecycle is owned by ``baldur.init()``
# (D7) and by ``reset_dlq_outbox`` for test isolation (D8).
_outbox: Outbox | None = None
_outbox_lock = threading.Lock()

# PID that built the singleton above. A mismatch means this process inherited
# the outbox across ``fork()`` and must re-own it before using it.
_outbox_origin_pid: int | None = None
_outbox_repair_gate = threading.Lock()

# Producer-side fail-open flag. Toggled by EventBus subscribers wired in
# ``setup_dlq_outbox()`` (impl 489 D8): the cross-shape ``DaemonWorkerProbe``
# emits ``DAEMON_WORKER_DIED`` on dead-thread detection (sets True) and
# ``DAEMON_WORKER_RESPAWNED`` on successful auto-restart (sets False).
_worker_dead: bool = False
_worker_dead_lock = threading.Lock()
_worker_dead_coercions: int = 0
_DLQ_OUTBOX_WORKER_NAME = "DLQOutboxWorker"
# Set by the process teardown alongside ``_worker_dead`` and never cleared for
# the life of the process. The RESPAWNED subscriber consults it: the probe can
# respawn a drainer that died *during* the teardown's optimistic flush phase
# (the stopping mark is only set later, inside ``worker.stop()``), and letting
# that respawn clear the coercion would route later captures back into a ring
# whose drainer is being joined and whose dump is about to run.
_teardown_started: bool = False

# Teardown once-guard. Deliberately NOT ``_outbox_lock``: that is the
# singleton-construction lock, and holding it across a blocking teardown would
# put a first-time ``get_outbox()`` build, a ``setup_dlq_outbox()`` re-entry and
# ``reset_dlq_outbox()`` behind the whole drain. A second caller blocks here and
# receives the first caller's cached result, because that result is the terminal
# report an exit hook logs — "ran nothing" would report zeros over a real drain.
_shutdown_gate = threading.Lock()
_shutdown_result: OutboxShutdownResult | None = None

# Floors carved out of the teardown budget. The dump is the safety net — it is
# what turns "lost" into "on disk" — so the optimistic flush phase ahead of it
# may not spend its share. First-come would let a slow flush starve the net to
# zero seconds, inverting the priority.
_MIN_STOP_JOIN_SECONDS = 0.5
_MIN_DUMP_SECONDS = 1.0

# Used only when the settings read itself fails; mirrors the shipped
# ``join_timeout_seconds`` default so a degenerate config still gets a bounded
# teardown rather than an unbounded one.
_FALLBACK_TEARDOWN_BUDGET_SECONDS = 5.0


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
        emergency_dump: (
            Callable[[list[dict[str, Any]], float | None], int] | None
        ) = None,
    ) -> Outbox:
        """Build an Outbox from ``DLQOutboxSettings`` with default wiring.

        ``sync_writer`` defaults to ``DLQService.store_failure(mode="sync", ...)``
        via lazy import. ``emergency_dump`` defaults to dispatching each
        kwargs through ``DLQService._write_to_local_fallback`` (D11.3).
        """
        from baldur.settings.dlq_outbox import get_dlq_outbox_settings

        settings = get_dlq_outbox_settings()

        # No ``on_drop_threshold``: the buffer's own callback fires under its
        # lock on the producer thread. Drop accounting and alerting belong to
        # the worker, which already wakes each cycle and blocks nobody.
        buffer: RingBuffer = RingBuffer(
            capacity=settings.capacity,
            strategy=BackpressureStrategy.DROP_OLDEST,
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
            on_drops_observed=_on_drops_observed,
            on_drop_alert=_on_drop_threshold,
            drop_rate_threshold=settings.drop_rate_threshold,
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
        accepted = self._buffer.put((time.monotonic(), kwargs))
        # D4 — queue-depth leading indicator, refreshed lazily on each put
        # (reads the lock-maintained RingBuffer size; fail-open on metric errors).
        try:
            from baldur.services.metrics.definitions import dlq_outbox_current_size

            dlq_outbox_current_size.set(self._buffer.size)
        except Exception:
            pass
        return accepted

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        self._worker.start()

    def stop(self, timeout: float = 5.0, dump_deadline: float | None = None) -> int:
        """Stop the writer and rescue what it did not drain.

        ``dump_deadline`` is an absolute ``time.monotonic()`` instant bounding
        the emergency dump; ``None`` leaves it unbounded. Returns the count of
        entries the dump reported having written.
        """
        return self._worker.stop(timeout=timeout, dump_deadline=dump_deadline)

    def repair_after_fork(self) -> None:
        """Re-own the buffer and the writer this process inherited across fork.

        Buffer first, writer second: the respawned writer starts draining the
        moment it exists, and it must find this process's own (empty) buffer
        rather than the copy of the parent's — whose entries the parent's own
        live drainer is still delivering.
        """
        self._buffer.reset_after_fork()
        self._worker.repair_after_fork()

    def flush_and_wait(self, timeout: float = 5.0) -> int:
        """Drain queued entries through the worker, blocking up to ``timeout``.

        Returns the ``entries_written`` delta observed across the wait. This
        method does NOT emergency-dump: entries still queued at the deadline
        are simply left in the buffer for ``stop()``, which owns the dump. The
        delta also omits entries the writer soft-failed or hard-failed, so it
        may not back any "nothing was lost" claim on its own — the terminal
        shutdown report is built from ``OutboxStats`` deltas instead.
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
            entries_soft_failed=self._worker.entries_soft_failed,
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


def _repair_if_forked() -> None:
    """Re-own the module singleton when this process inherited it via ``fork()``.

    Runs at the head of both entry points that reach the singleton, *ahead* of
    their ``_outbox is not None`` early returns — that is the whole point.
    Without it neither entry point is reachable in a fork child: the inherited
    singleton short-circuits ``get_outbox()`` into returning a producer whose
    drainer is dead, and ``setup_dlq_outbox()`` into ``False`` without ever
    calling ``start()``. Every async DLQ store in that child then reports
    success into a buffer nothing consumes.

    Both module locks are renewed before anything acquires them: the forking
    process holds ``_outbox_lock`` across the whole build, so a fork taken at
    that instant hands the child a lock no thread here will ever release. The
    repair gate itself is entered only after an unlocked mismatch pre-check, so
    the process that owns the state never holds it and it is never inherited
    locked; inside, the mismatch is re-checked so a second thread returns
    rather than repairing twice.

    ``_worker_dead`` restarts False: the flag describes the parent's drainer,
    and coercing every store to the sync writer on that basis would cost the
    child the async path it is about to get back.
    """
    global _outbox_lock, _outbox_origin_pid
    global _worker_dead, _worker_dead_lock, _worker_dead_coercions
    global _teardown_started

    origin = _outbox_origin_pid
    if origin is None or origin == os.getpid():
        return

    with _outbox_repair_gate:
        inherited_pid = _outbox_origin_pid
        if inherited_pid is None or inherited_pid == os.getpid():
            return  # another thread finished the repair first

        _outbox_lock = threading.Lock()
        _worker_dead_lock = threading.Lock()
        _worker_dead = False
        _worker_dead_coercions = 0
        _teardown_started = False

        inherited = _outbox
        _outbox_origin_pid = os.getpid()

    if inherited is not None:
        inherited.repair_after_fork()

    logger.info("dlq_outbox.fork_state_repaired", inherited_pid=inherited_pid)


@fork_repaired(repair=_repair_if_forked)
def get_outbox() -> Outbox:
    """Return the process-singleton outbox, building lazily on first call.

    The eager-start path runs through ``setup_dlq_outbox`` from
    ``baldur.init()``; this lazy path covers tests / scripts that touch the
    DLQ store before init(). Fork-aware: defense in depth for the producer,
    which reaches the singleton without going through the starter.
    """
    global _outbox, _outbox_origin_pid
    if _outbox is not None:
        return _outbox
    with _outbox_lock:
        if _outbox is not None:
            return _outbox
        _outbox = Outbox.from_settings()
        _outbox.start()
        _outbox_origin_pid = os.getpid()
        return _outbox


@fork_repaired(repair=_repair_if_forked)
def setup_dlq_outbox() -> bool:
    """Eager-start hook called from ``baldur.init()`` (D7).

    Idempotent. Returns True on first start, False on re-entry — including in
    a fork child, where the decorator has already re-owned and respawned the
    inherited singleton by the time the early return is reached. "False" there
    means "this process did not build one", not "nothing was started".

    Also wires the two ``DAEMON_WORKER_*`` EventBus subscribers (impl 489
    D8): when the cross-shape ``DaemonWorkerProbe`` reports the
    ``DLQOutboxWorker`` daemon thread as dead, ``_worker_dead`` flips True
    and producer-side ``Outbox.put`` calls coerce to the sync writer
    (preserves D11.4 fail-open). On a successful auto-respawn,
    ``_worker_dead`` flips back False so the async fast path resumes.
    """
    global _outbox, _outbox_origin_pid
    with _outbox_lock:
        if _outbox is not None:
            return False
        _outbox = Outbox.from_settings()
        _outbox.start()
        _outbox_origin_pid = os.getpid()
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
    """Clear the producer fail-open flag on successful DLQOutboxWorker respawn.

    Not once the process teardown has begun: a drainer respawned into a
    teardown is one the teardown is about to join and dump, and clearing the
    coercion would hand later captures to a buffer nothing will read again.
    """
    global _worker_dead
    data = getattr(event, "data", None) or {}
    if data.get("worker_name") != _DLQ_OUTBOX_WORKER_NAME:
        return
    with _worker_dead_lock:
        if _teardown_started:
            logger.debug("dlq_outbox.respawn_coercion_clear_skipped_teardown")
            return
        _worker_dead = False


def reset_dlq_outbox() -> int:
    """Drain pending entries, stop the worker, clear state.

    Wired into ``baldur.protect_facade.reset_protect_caches`` (D8). MUST drain
    rather than just clear so queued entries from the prior test do not
    surface in the next test's worker.
    """
    global _outbox, _outbox_origin_pid, _worker_dead, _worker_dead_coercions
    global _shutdown_result, _teardown_started

    # The teardown's cached result describes an outbox this call is discarding.
    # Kept, the next process-lifetime teardown would return the previous one's
    # counts without draining anything.
    with _shutdown_gate:
        _shutdown_result = None

    with _outbox_lock:
        if _outbox is None:
            with _worker_dead_lock:
                _worker_dead = False
                _worker_dead_coercions = 0
                _teardown_started = False
            return 0
        # Best-effort: give the worker a short window to drain before stop.
        try:
            _outbox.flush_and_wait(timeout=1.0)
        except Exception:
            pass
        remaining = _outbox.stop(timeout=1.0)
        _outbox = None
        _outbox_origin_pid = None

    with _worker_dead_lock:
        _worker_dead = False
        _worker_dead_coercions = 0
        _teardown_started = False
    return remaining


def get_shutdown_reserve_seconds() -> float:
    """Seconds an exit path must hold back for the outbox teardown.

    A step that waits on other subsystems has to reserve the budget of the step
    behind it, or the teardown is the first thing an external watcher cuts —
    and it is the step that turns buffered entries into persisted ones. The
    reserve is the configured teardown budget plus the dump's floor, which is
    the teardown's worst case.

    Exposed as a function rather than by publishing the floors, so the split
    stays owned by this module.
    """
    try:
        from baldur.settings.dlq_outbox import get_dlq_outbox_settings

        budget = get_dlq_outbox_settings().join_timeout_seconds
    except Exception as e:
        logger.warning("dlq_outbox.teardown_budget_read_failed", error=e)
        budget = _FALLBACK_TEARDOWN_BUDGET_SECONDS
    return budget + _MIN_DUMP_SECONDS


def stop_outbox_for_shutdown(timeout: float | None = None) -> OutboxShutdownResult:
    """Tear the process outbox down once, and report what happened to its entries.

    The single idempotent teardown every exit path calls unconditionally: the
    shutdown coordinator's handler on a signalled exit, and each adapter's exit
    hook on a recycle exit, which has no coordinator window at all. Repeat calls
    block on the gate and return the first caller's cached result.

    Args:
        timeout: Total teardown budget in seconds. ``None`` reads
            ``DLQOutboxSettings.join_timeout_seconds``. Split three ways with
            floors, so the emergency dump cannot be starved by the phases ahead
            of it: flush gets ``budget - join floor - dump floor``, the join
            gets whatever is left above its floor, and the dump's deadline is
            floored at ``_MIN_DUMP_SECONDS`` past the join.

    Returns:
        The terminal counts. Built from ``OutboxStats`` deltas rather than from
        the primitives' return values — ``flush_and_wait`` returns a written
        delta that silently omits failed entries, and no completion claim may
        rest on it.
    """
    global _shutdown_result, _worker_dead, _teardown_started

    with _shutdown_gate:
        if _shutdown_result is not None:
            return _shutdown_result

        # 1. Coerce producers to the synchronous writer FIRST, before anything
        #    else and whether or not an outbox exists. Set last, there is a
        #    window between the dump and the flag write in which a capture
        #    lands in a buffer whose drainer has been joined and whose dump has
        #    already run — that entry dies with the process, which is the exact
        #    failure this teardown exists to remove. Ahead of the ``is None``
        #    return for the same reason: a process that builds the outbox
        #    lazily after the teardown began would otherwise get an undrained
        #    buffer.
        with _worker_dead_lock:
            _worker_dead = True
            _teardown_started = True

        outbox = _outbox
        if outbox is None:
            # Nothing was built in this process: nothing to drain, nothing to
            # report. An all-zero result, not a fabricated drain.
            _shutdown_result = OutboxShutdownResult(0, 0, 0, 0, 0, 0, 0)
            return _shutdown_result

        if timeout is None:
            try:
                from baldur.settings.dlq_outbox import get_dlq_outbox_settings

                timeout = get_dlq_outbox_settings().join_timeout_seconds
            except Exception as e:
                logger.warning("dlq_outbox.teardown_budget_read_failed", error=e)
                timeout = _FALLBACK_TEARDOWN_BUDGET_SECONDS

        started = time.monotonic()
        before = outbox.get_stats()
        worker = outbox.worker

        # Waiting cannot help when the drainer is not alive, or when it is in
        # sustained backoff: both mean the buffer will not empty by waiting, and
        # the remainder goes to the dump either way. Skipping is not a
        # concession — a backing-off drainer is one whose writes are already
        # failing, so waking it later fails the same writes.
        if worker.is_alive and not worker.is_backing_off:
            flush_share = max(0.0, timeout - _MIN_STOP_JOIN_SECONDS - _MIN_DUMP_SECONDS)
            if flush_share > 0:
                try:
                    outbox.flush_and_wait(timeout=flush_share)
                except Exception as e:
                    logger.warning("dlq_outbox.teardown_flush_failed", error=e)

        now = time.monotonic()
        # The dump's floor is not spendable by the join either.
        join_share = max(
            started + timeout - now - _MIN_DUMP_SECONDS, _MIN_STOP_JOIN_SECONDS
        )
        dump_deadline = max(started + timeout, now + join_share + _MIN_DUMP_SECONDS)

        try:
            outbox.stop(timeout=join_share, dump_deadline=dump_deadline)
        except Exception as e:
            logger.warning("dlq_outbox.teardown_stop_failed", error=e)

        after = outbox.get_stats()
        pending_at_entry = before.size + before.in_flight
        # A producer that passed the coercion check before the flag flipped can
        # still ``put`` into a full ring during the teardown; DROP_OLDEST then
        # evicts an entry that was pending at entry and is in no bucket below
        # (the newcomer takes its slot, so the relation still balances). The
        # drainer that would normally observe the drop window may be dead, so
        # this is the only place the substitution can be reported.
        dropped_during_teardown = max(0, after.total_dropped - before.total_dropped)
        if dropped_during_teardown:
            logger.warning(
                "dlq_outbox.teardown_drops_observed",
                dropped=dropped_during_teardown,
                pending_at_entry=pending_at_entry,
            )
        dispatched = max(0, after.entries_written - before.entries_written)
        soft_failed = max(0, after.entries_soft_failed - before.entries_soft_failed)
        failed = max(0, after.entries_failed - before.entries_failed)
        emergency_dumped = max(
            0, after.entries_emergency_dumped - before.entries_emergency_dumped
        )
        residual = worker.entries_shutdown_residual
        accounted = dispatched + soft_failed + failed + emergency_dumped + residual
        _shutdown_result = OutboxShutdownResult(
            pending_at_entry=pending_at_entry,
            dispatched=dispatched,
            soft_failed=soft_failed,
            failed=failed,
            emergency_dumped=emergency_dumped,
            residual=residual,
            # The difference the conservation relation names. Reported rather
            # than hidden, so an operator reading a bucket sum larger than the
            # pending count sees why instead of filing a bug.
            duplicated=max(0, accounted - pending_at_entry),
        )
        return _shutdown_result


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
    """Resolve the DLQ capture backing and dispatch the kwargs synchronously.

    Resolves the single backing chain (PRO ``DLQService`` under ACTIVE
    entitlement, else the OSS ``DLQCaptureService``) and calls
    ``store_failure(mode="sync", ...)`` — never ``repository.create`` directly,
    so validation / masking / truncation / overflow / local-fallback all apply.
    Lives in the worker thread, so the backing-resolution cost stays entirely
    off the producer hot path.
    """
    from baldur.services.dlq_capture import resolve_dlq_backing

    return resolve_dlq_backing().store_failure(mode="sync", **kwargs)


def _default_emergency_dump(
    batch: list[dict[str, Any]],
    deadline: float | None = None,
) -> int:
    """Dispatch each remaining entry through the backing's local fallback.

    Reuses the existing zero-loss disk fallback — no new dump format
    introduced. Called only on shutdown timeout when the worker cannot drain in
    time. Resolves the same backing chain as the sync writer.

    Args:
        batch: Remaining entry kwargs, in enqueue order.
        deadline: Absolute ``time.monotonic()`` instant this loop may not run
            past. ``None`` runs unbounded. Checked BEFORE each entry rather
            than per batch: the fallback's file tier does an
            open/write/flush/fsync per entry under a class-level lock, so at
            network-storage fsync latencies a single entry is the granularity
            that matters. A deadline can only be honoured at a yield point, so
            a write already in progress still overshoots it by one write.

    Returns:
        The number of entries this call actually wrote. Never the batch size:
        an unresolvable backing writes nothing, and the caller has to be able
        to tell that apart from a completed dump.
    """
    try:
        from baldur.services.dlq_capture import resolve_dlq_backing

        service = resolve_dlq_backing()
    except Exception as e:
        logger.warning("dlq_outbox.emergency_dump_unavailable", error=e)
        return 0

    # ``_write_to_local_fallback`` is the zero-loss disk-fallback path; both the
    # OSS ``DLQCaptureService`` base and the PRO overlay expose it. getattr keeps
    # the reach-through defensive across any backing shape.
    fallback = getattr(service, "_write_to_local_fallback", None)
    if fallback is None:
        logger.warning(
            "dlq_outbox.emergency_dump_unsupported",
            reason="DLQService does not expose _write_to_local_fallback",
        )
        return 0

    written = 0
    for kwargs in batch:
        if deadline is not None and time.monotonic() >= deadline:
            break
        try:
            # The fallback returns the destination it stored to, or None when
            # every tier failed. Counting the call rather than its answer would
            # report a failed write as a rescue.
            if fallback(kwargs, "shutdown_emergency_dump"):
                written += 1
        except Exception as e:
            logger.exception("dlq_outbox.emergency_dump_entry_failed", error=e)
    return written


# =============================================================================
# Drop accounting + drop-rate alert callbacks (D4, reworked by 779 D12)
# =============================================================================


def _on_drops_observed(dropped: int) -> None:
    """Count a drain cycle's dropped entries into ``dlq_outbox_drops_total``.

    Summed per window, so the counter reads as the number of entries actually
    lost — not as the number of times an alert happened to fire.
    """
    try:
        from baldur.services.metrics.definitions import dlq_outbox_drops_total

        dlq_outbox_drops_total.labels(domain="default").inc(dropped)
    except Exception:
        pass


def _on_drop_threshold(window: DropWindow) -> None:
    """Worker-side windowed drop-rate alert.

    1. WARNING log
    2. DLQ_OUTBOX_DROP_THRESHOLD_BREACHED EventBus event

    The entry count is published by :func:`_on_drops_observed` every cycle, so
    this stays purely an alert — re-armable per window, unlike the once-per-
    process latch it replaces.
    """
    logger.warning(
        "dlq.outbox_drop_threshold_breached",
        capacity=window.capacity,
        size=window.size,
        dropped_in_window=window.dropped,
        enqueued_in_window=window.enqueued,
        total_dropped=window.total_dropped,
        drop_rate=window.drop_rate,
    )

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
                "capacity": window.capacity,
                "size": window.size,
                "dropped_in_window": window.dropped,
                "enqueued_in_window": window.enqueued,
                "total_dropped": window.total_dropped,
                "drop_rate": window.drop_rate,
            },
            source="dlq_outbox",
            priority=EventPriority.HIGH,
        )
    except Exception as e:
        logger.debug("dlq_outbox.drop_event_emit_failed", error=e)


def _on_processing_delay(delay_seconds: float, domain: str) -> None:
    """Worker-side enqueue→pop delay observation (D4 leading indicator).

    The stored DLQ domain goes through the resolution funnel so this histogram
    carries the same canonical, cap-enforced label as every other DLQ family.
    """
    try:
        from baldur.metrics.registry import resolve_domain_label
        from baldur.services.metrics.definitions import (
            dlq_outbox_processing_delay_seconds,
        )

        domain = resolve_domain_label(domain)
        dlq_outbox_processing_delay_seconds.labels(domain=domain).observe(delay_seconds)
    except Exception:
        pass
