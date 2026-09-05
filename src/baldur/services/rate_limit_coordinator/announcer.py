"""Verified cooldown-end announcement for the rate limit coordinator.

The all-clear a fleet acts on has to be a fact, not a prediction. A worker that
observes a 429 learns the effective expiry the shared store merged its candidate
into; a *peer's* later 429 extends that expiry through the store and reaches this
process through no event at all. Announcing at the locally learned time therefore
releases outbound throughput while the provider is still rate-limiting the fleet
— the self-DDoS the coordinator exists to prevent.

``CooldownAnnouncer`` keeps one *record* per key — the effective expiry this
process last learned — and one daemon thread per process that wakes on the
nearest record, re-reads the shared store, and announces only what the store
itself reports as ended. A record whose store value is still in the future is
learned, never announced: that read is the only path by which a peer's extension
reaches this process, and it needs no event, no subscription and no particular
event-bus backend.

Verification depends on the storage adapter telling "no cooldown" apart from
"cannot tell". That is what ``RateLimitStorageInterface.get_state_strict``
exists for, and every adapter shipped with Baldur overrides it so backend
failures propagate. A bring-your-own adapter that does not override it inherits
the folding default, and its all-clear is only as verified as that adapter — the
storage interface states the same contract on the method itself.

Lifecycle notes:

- The thread starts lazily, on the first key this process tracks. A process that
  never observes a 429 runs no thread and registers no daemon worker.
- While the store cannot be read, *every* announcement in the process is held
  until the next probe interval. "Cannot tell" must never read as "ended", so
  the announcement is at most one interval late and never early.
- ``fork()`` children re-own the announcer on first entry: fresh lock, wake
  event and thread, inherited records kept (a record is only a wake hint, and it
  is verified against the store before it is acted on).
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

import structlog

from baldur.core.process_utils import fork_repaired

from .helpers import _emit_rate_limit_event

if TYPE_CHECKING:
    from baldur.interfaces.rate_limit_storage import RateLimitStorageInterface
    from baldur.meta.daemon_worker import DaemonWorkerHandle

logger = structlog.get_logger()

__all__ = ["CooldownAnnouncer"]

#: Registered daemon-worker name — also the thread name and the ``name`` label
#: of the ``baldur_daemon_worker_*`` liveness series for this thread.
DAEMON_WORKER_NAME = "rate_limit_cooldown_announcer"

#: Longest the loop sleeps between iterations, regardless of how far away the
#: nearest record is. Not a setting: it bounds liveness granularity only —
#: wake-on-expiry makes the announcement timing independent of it — while the
#: registered handle declares this value as its heartbeat cadence, so a loop
#: that slept until a one-hour expiry would read as a dead worker.
_TICK_INTERVAL_SECONDS: float = 1.0

#: Symmetric jitter applied to the held-read retry interval, so a fleet that
#: lost its store together does not re-read it in lockstep.
_HOLD_JITTER_RATIO: float = 0.2

#: Join ceiling on ``stop()``. The wake event interrupts the loop's sleep in
#: milliseconds, so this only ever trips on an iteration blocked inside a store
#: read, where abandoning the daemon thread is the correct outcome.
_STOP_JOIN_TIMEOUT_SECONDS: float = 5.0


class _AnnouncerState:
    """Everything a ``fork()`` child must not inherit as-is, in one object.

    Held behind a single attribute so a child re-owns the whole thing with one
    store and no lock: a repair gate would be held exactly by a child mid-repair,
    and a grandchild forked in that window would inherit it held forever.
    """

    __slots__ = (
        "held_until",
        "hold_delay",
        "in_flight",
        "lock",
        "origin_pid",
        "records",
        "scan_offset",
        "spawn_lock",
        "stopped",
        "thread",
        "wake",
    )

    def __init__(self, records: dict[str, float] | None = None) -> None:
        self.lock = threading.Lock()
        # Separate from ``lock`` on purpose: the spawn registers a daemon worker
        # and reads settings, and serializing that behind the lock every record
        # write takes would put the registry's lock underneath this one.
        self.spawn_lock = threading.Lock()
        self.wake = threading.Event()
        self.thread: threading.Thread | None = None
        self.records: dict[str, float] = dict(records) if records else {}
        # Deliberately NOT inherited across a fork: ``begin()`` is paired with a
        # ``track()`` in a ``finally`` on a request thread that does not exist in
        # the child, so an inherited marker is never decremented and would skip
        # its key for the life of the process.
        self.in_flight: dict[str, int] = {}
        self.held_until: float | None = None
        self.hold_delay: float | None = None
        self.scan_offset = 0
        self.stopped = False
        self.origin_pid = os.getpid()


class CooldownAnnouncer:
    """Announces ``RATE_LIMIT_COOLDOWN_END`` only for cooldowns the store says
    have ended.

    One instance per coordinator, one daemon thread per instance, started by the
    first tracked key. Callers do not construct this directly — the coordinator
    owns it and brackets its own store write with ``begin()`` / ``track()``.
    """

    def __init__(
        self,
        storage: RateLimitStorageInterface,
        *,
        emit: Callable[..., None] = _emit_rate_limit_event,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """
        Args:
            storage: the shared rate-limit store this announcer verifies against.
            emit: event-emission seam, defaulting to the coordinator's fail-open
                EventBus helper. Tests pass a recorder.
            clock: wall-clock source, defaulting to ``time.time`` — the same
                clock the stored expiries are written against.
        """
        self._storage = storage
        self._emit = emit
        self._clock = clock
        self._handle: DaemonWorkerHandle | None = None
        self._state = _AnnouncerState()

    # =========================================================================
    # Fork re-ownership
    # =========================================================================

    def _repair_if_forked(self) -> None:
        """Re-own the announcer's state when this process inherited it via fork.

        Lock-free on purpose: a lock taken here would be held exactly by a
        process whose pid mismatches — a child mid-repair — so a grandchild
        forked in that window would inherit it held and block forever. The whole
        state is swapped instead, so a thread that loaded another thread's fresh
        object simply works from that one.

        The records survive: a record is a wake hint that is verified against the
        store before anything is announced, so an inherited one is either
        truthful or harmlessly late, and dropping it would strand a consumer that
        also inherited "a 429 is in force" from the same parent. Everything else
        — the lock, the wake event, the thread, the hold and the in-flight
        markers — belongs to the parent and starts fresh.
        """
        inherited = self._state
        if inherited.origin_pid == os.getpid():
            return

        self._state = _AnnouncerState(records=inherited.records)
        handle = self._handle
        if handle is not None:
            handle.reset_after_fork()

    # =========================================================================
    # Public entry points
    # =========================================================================

    @fork_repaired
    def begin(self, key: str) -> None:
        """Mark a local 429 for ``key`` as in flight.

        Called immediately before the coordinator's store write. Between that
        write landing and the matching ``track()`` the process record still
        carries the *old* expiry, so a verifying read in that window would see
        the pre-429 store value it was armed for and announce into the cooldown
        this 429 is installing. The marker makes the loop skip the key instead.
        """
        state = self._state
        with state.lock:
            state.in_flight[key] = state.in_flight.get(key, 0) + 1

    @fork_repaired
    def track(self, key: str, cooldown_until: float | None = None) -> None:
        """Record ``key``'s effective expiry and release the in-flight marker.

        Called from a ``finally``, so a store that raised releases the marker
        without recording anything. Replace rather than merge: the caller passes
        the value the store itself returned, and an operator ``clear()`` followed
        by a shorter 429 must be able to move the record earlier.
        """
        state = self._state
        with state.lock:
            remaining = state.in_flight.get(key, 0) - 1
            if remaining > 0:
                state.in_flight[key] = remaining
            else:
                state.in_flight.pop(key, None)
            if cooldown_until is not None and not state.stopped:
                state.records[key] = cooldown_until
            has_records = bool(state.records)
        state.wake.set()
        if has_records:
            self._ensure_thread()

    @fork_repaired
    def reverify(self, key: str) -> None:
        """Bring ``key``'s next verification forward, after an operator clear.

        A key this process holds no record for is left alone: announcing there
        would release a cooldown that never cooled here, which is the failure the
        record-scoped announcement exists to avoid.
        """
        state = self._state
        with state.lock:
            current = state.records.get(key)
            if current is None:
                return
            state.records[key] = min(current, self._clock())
        state.wake.set()
        self._ensure_thread()

    @fork_repaired
    def ensure_running(self) -> None:
        """Revive the announcer thread from the request path.

        The entry points above only run on a 429 or an operator clear, and a
        cooldown is precisely the window in which this process makes neither
        call. Auto-respawn is opt-in and off by default, so without this poke a
        thread that died — or a fork child that inherited records and no thread —
        would hold its records until the next 429 the cooldown is preventing.
        """
        state = self._state
        if state.stopped or not state.records:
            return
        thread = state.thread
        if thread is not None and thread.is_alive():
            return
        self._ensure_thread()

    @fork_repaired
    def run_once(self, now: float | None = None) -> list[str]:
        """Run one verification pass over the records that are due.

        Returns:
            The keys announced in this pass, in announcement order.
        """
        current = self._clock() if now is None else now

        due = self._due_records(current)
        announced: list[str] = []
        for expiry, key in due:
            try:
                verified_until = self._verify(key, expiry, current)
            except Exception as e:
                # The whole per-key step is guarded, not just the read: an
                # adapter can hand back a state whose ``cooldown_until`` the
                # comparison below cannot use (a NULL column, a value some other
                # tool wrote) and that raises from the comparison, not the read.
                self._enter_hold(key, e)
                break
            # Only here — after the comparison, not after the read. A store
            # that answers with a value the comparison cannot use raises from
            # inside the step, and clearing the hold on the read alone would
            # redraw the interval and re-report the edge on every pass while
            # announcing a recovery that never happened.
            self._leave_hold()
            if verified_until is None:
                continue
            # Emitted outside the announcer's lock: a subscriber may hold the
            # dispatch for the bus handler timeout, and on the distributed bus
            # the publish is a socket write on this thread — holding the lock
            # across either would stall a request thread's ``begin()``.
            self._emit(
                "RATE_LIMIT_COOLDOWN_END",
                {
                    "key": key,
                    "cooldown_ended_at": self._clock(),
                    "cooldown_until": verified_until,
                },
                priority_name="NORMAL",
            )
            logger.info(
                "rate_limit_coordinator.cooldown_ended",
                rate_limit_key=key,
            )
            announced.append(key)
        return announced

    @fork_repaired
    def stop(self) -> None:
        """Stop the announcer thread and forbid any further announcement.

        The join is a ceiling, not a guarantee — an iteration already blocked
        inside a store read outlives it — so permission is withdrawn before the
        join rather than by it: the records are cleared and ``stopped`` is set,
        and a thread returning late from its read finds neither a record to
        announce nor permission to emit.
        """
        state = self._state
        handle = self._handle
        # Set first, so a liveness probe running concurrently with the join reads
        # the handle as STOPPING rather than reporting a dead worker.
        if handle is not None:
            handle.is_stopping = True

        with state.lock:
            state.stopped = True
            state.records.clear()
        state.wake.set()

        thread = state.thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=_STOP_JOIN_TIMEOUT_SECONDS)
            if thread.is_alive():
                logger.critical(
                    "daemon_worker.stop_join_timeout",
                    worker_name=DAEMON_WORKER_NAME,
                    join_timeout_seconds=_STOP_JOIN_TIMEOUT_SECONDS,
                )
        state.thread = None

        if handle is not None:
            from baldur.metrics.recorders.daemon_worker import unregister_daemon_worker

            unregister_daemon_worker(DAEMON_WORKER_NAME)
            self._handle = None

    @property
    @fork_repaired
    def pending(self) -> dict[str, float]:
        """Snapshot of the records this process currently holds."""
        state = self._state
        with state.lock:
            return dict(state.records)

    @property
    @fork_repaired
    def is_alive(self) -> bool:
        """Whether this process has a live announcer thread."""
        thread = self._state.thread
        return thread is not None and thread.is_alive()

    # =========================================================================
    # Verification pass
    # =========================================================================

    def _due_records(self, now: float) -> list[tuple[float, str]]:
        """The records due at ``now``, earliest first, rotated by one per pass.

        The rotation is what keeps a key the store can never return a usable
        value for from starving the others: unrotated it is always the earliest
        record, so every pass would break on it and nothing else would ever be
        announced.
        """
        state = self._state
        with state.lock:
            if state.stopped:
                return []
            if state.held_until is not None and now < state.held_until:
                return []
            due = sorted(
                (expiry, key)
                for key, expiry in state.records.items()
                if expiry <= now and not state.in_flight.get(key)
            )
            if not due:
                return []
            offset = state.scan_offset % len(due)
            state.scan_offset = offset + 1
        return due[offset:] + due[:offset]

    def _verify(self, key: str, expiry: float, now: float) -> float | None:
        """Decide ``key`` against the store.

        Returns:
            The store's expiry when the all-clear is owed, otherwise ``None``.
            A record still in force is moved to the store's later value in place
            — the only path by which a peer's extension reaches this process.
        """
        state = self._state
        stored_until = self._storage.get_state_strict(key).cooldown_until

        if stored_until > now:
            with state.lock:
                current = state.records.get(key)
                if current is not None:
                    state.records[key] = max(current, stored_until)
            logger.debug(
                "rate_limit_announcer.peer_extension_learned",
                rate_limit_key=key,
                previous=expiry,
                learned=stored_until,
            )
            return None

        with state.lock:
            if state.stopped or state.in_flight.get(key):
                return None
            if state.records.get(key) != expiry:
                return None
            del state.records[key]
        return stored_until

    def _enter_hold(self, key: str, error: BaseException) -> None:
        """Hold every announcement in this process until the next probe.

        Whole-process rather than per-key: an unreadable store answers no key,
        and a key whose stored value is unusable answers on the same cadence for
        the same reason — one hold state instead of two, priced at one interval's
        delay for the other due keys.
        """
        state = self._state
        with state.lock:
            edge = state.hold_delay is None
            if state.hold_delay is None:
                state.hold_delay = self._draw_hold_delay()
            state.held_until = self._clock() + state.hold_delay
            delay = state.hold_delay

        if edge:
            logger.warning(
                "rate_limit_announcer.store_read_failed",
                rate_limit_key=key,
                retry_in_seconds=delay,
                error=str(error),
            )
        else:
            # Intermediate retry failures inside an outage already reported at
            # its edge — one WARNING per outage, not one per probe.
            logger.debug(
                "rate_limit_announcer.store_read_failed",
                rate_limit_key=key,
                retry_in_seconds=delay,
                error=str(error),
            )

    def _leave_hold(self) -> None:
        """Clear the hold after a read the store actually answered."""
        state = self._state
        with state.lock:
            if state.held_until is None and state.hold_delay is None:
                return
            state.held_until = None
            state.hold_delay = None
        logger.info("rate_limit_announcer.store_read_recovered")

    def _draw_hold_delay(self) -> float:
        """Draw this outage's retry interval, jittered, once per hold.

        The strict read always attempts the real backend, so it *is* a recovery
        probe and must not out-pace the storage adapter's own gated one — the
        interval is that adapter's probe interval, drawn on the same symmetric
        window.
        """
        from baldur.settings.rate_limit import get_rate_limit_settings
        from baldur.utils.jitter import calculate_jitter

        interval = float(
            get_rate_limit_settings().redis_recovery_probe_interval_seconds
        )
        return calculate_jitter(
            min_delay_seconds=interval * (1.0 - _HOLD_JITTER_RATIO),
            max_delay_seconds=interval * (1.0 + _HOLD_JITTER_RATIO),
        )

    # =========================================================================
    # Thread lifecycle
    # =========================================================================

    def _ensure_thread(self) -> None:
        """Spawn the announcer thread unless this process already has a live one."""
        state = self._state
        if state.stopped:
            return
        thread = state.thread
        if thread is not None and thread.is_alive():
            return
        try:
            self._spawn_thread()
        except RuntimeError as e:
            # Refused at interpreter shutdown and at a live process's thread
            # ceiling. The records are kept, so the next entry point retries.
            logger.warning(
                "rate_limit_announcer.spawn_failed",
                error=str(e),
            )

    def _spawn_thread(self) -> None:
        """Construct + start a fresh announcer thread.

        Also the handle's restart callback, so it repairs a forked state first
        and consults no running flag — a liveness probe can reach this through
        the inherited handle before any entry point has run in a fork child.

        Deliberately not gated on the fork-source predicate: that predicate
        answers True in every worker of a deployment whose operator never wired
        the pre-fork server's post-fork hook, and this spawn is demand-driven
        with no startup re-entry to fall back on. Spawning in a process that
        later forks costs one thread that dies at ``fork()``; the children re-own
        the records and start their own.
        """
        self._repair_if_forked()
        state = self._state

        # The aliveness test at every entry point is a check-then-act, and
        # ``Thread.start()`` sits inside the window it leaves open: a 429 storm
        # is exactly the moment several request threads reach ``track()``
        # together, and each one that read the empty slot would start a loop of
        # its own — permanently, since a loop exits only on ``stop()``. The
        # re-check under this lock is what makes "one thread per process" true.
        with state.spawn_lock:
            if state.stopped:
                return
            running = state.thread
            if running is not None and running.is_alive():
                return

            thread = threading.Thread(
                target=self._loop_with_crash_capture,
                name=DAEMON_WORKER_NAME,
                daemon=True,
            )
            thread.start()
            state.thread = thread

            self._register_handle(thread)

        logger.info("rate_limit_announcer.started")

    def _register_handle(self, thread: threading.Thread) -> None:
        """Register this process's handle, or rebind it onto a fresh thread."""
        handle = self._handle
        if handle is None:
            from baldur.meta.daemon_worker import DaemonWorkerHandle
            from baldur.metrics.recorders.daemon_worker import register_daemon_worker

            handle = DaemonWorkerHandle(
                thread=thread,
                tick_interval_seconds=_TICK_INTERVAL_SECONDS,
                staleness_threshold_seconds=self._staleness_threshold(),
                restart_callback=self._spawn_thread,
            )
            self._handle = handle
            register_daemon_worker(DAEMON_WORKER_NAME, handle)
        else:
            # The handle's identity has to survive a respawn — the registry entry
            # and the restart callback keep pointing at it — so only the thread
            # reference is rebound.
            handle.thread = thread

    def _staleness_threshold(self) -> float:
        """Seconds of heartbeat silence that mean this worker is really stuck.

        One iteration legitimately pays a store read and, when it announces, a
        bus dispatch — and on the distributed bus the publish is a socket write
        on this thread too. Derived from those budgets rather than from the tick,
        so a single blocked read or a slow subscriber does not report as a dead
        worker while a genuinely hung backend still does.
        """
        from baldur.settings.daemon_worker import get_daemon_worker_settings
        from baldur.settings.event_bus import get_event_bus_settings
        from baldur.settings.redis import get_redis_settings

        redis_settings = get_redis_settings()
        bus_settings = get_event_bus_settings()
        # One retry is configured on timeout, so the read budget is a connect
        # plus two socket timeouts.
        read_budget = (
            redis_settings.socket_connect_timeout + 2 * redis_settings.socket_timeout
        )
        publish_budget = (
            2 * redis_settings.socket_timeout
            if bus_settings.backend == "redis"
            else 0.0
        )
        return (
            _TICK_INTERVAL_SECONDS
            * get_daemon_worker_settings().default_staleness_multiplier
            + read_budget
            + bus_settings.handler_timeout_seconds
            + publish_budget
        )

    def _loop_with_crash_capture(self) -> None:
        try:
            self._loop()
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as e:
            if self._handle is not None:
                self._handle.record_crash(e)
            raise

    def _loop(self) -> None:
        """Verify, heartbeat, sleep until the nearest record or the next tick."""
        state = self._state
        while not state.stopped:
            # Cleared before the pass, so a wake armed during it is not consumed
            # by the wait that follows.
            state.wake.clear()
            started_at = time.monotonic()
            try:
                self.run_once()
            except Exception as e:
                logger.warning(
                    "rate_limit_announcer.pass_failed",
                    error=str(e),
                )

            handle = self._handle
            if handle is not None:
                handle.observe_iteration(time.monotonic() - started_at)
                handle.heartbeat()

            state.wake.wait(timeout=self._next_wait(state))
        logger.info("rate_limit_announcer.stopped")

    def _next_wait(self, state: _AnnouncerState) -> float:
        """Seconds to sleep: to the nearest deadline, clamped into the tick.

        Clamping at zero matters as much as clamping at the tick: a deadline
        already in the past would otherwise turn the wait into a busy loop for
        the length of an outage.
        """
        now = self._clock()
        with state.lock:
            if state.held_until is not None:
                deadline: float | None = state.held_until
            else:
                due = [
                    expiry
                    for key, expiry in state.records.items()
                    if not state.in_flight.get(key)
                ]
                deadline = min(due) if due else None
        if deadline is None:
            return _TICK_INTERVAL_SECONDS
        return min(max(deadline - now, 0.0), _TICK_INTERVAL_SECONDS)
