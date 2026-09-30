"""Per-process refresher for control state kept in the shared state store.

Operator switches (the kill switch, dry-run, the emergency level) live in the
shared state store, and every process acts on its own copy of them. A copy that
only the writing process refreshes leaves every other process on the old value
until it restarts. ``ControlStateRefresher`` is the delivery bound: one daemon
thread per process re-reads each registered key on its own interval and hands
the read to the key's owner, which assigns it to its copy.

A manager registers a key with an interval and a refresh callback. The callback
receives the store, performs its own strict read and assignment, and raises
when the read fails. The refresher owns everything else:

- scheduling — the thread wakes on the nearest due key;
- per-key read health — last success, consecutive failures, last error — with
  one WARNING per failure episode and one INFO line on recovery;
- the ``baldur_control_state_refreshed_timestamp_seconds{key}`` gauge, a
  last-success timestamp, so a stalled refresher cannot report a fresh age;
- backoff after consecutive failures, from the key's interval up to a cap —
  building the store runs under the runtime-wide singleton lock, and the cap
  bounds how often an unanswering store takes it.

Refresh passes run one at a time under a pass lock that only passes take — the
thread's ticks, the synchronous pass at startup and ``refresh_now()``. Readers
and writers of a copy never take it, so a hung store read ages the copy; it
never delays a call.

Lifecycle notes:

- ``fork()`` children re-own the lifecycle state on first entry: fresh locks,
  wake event, thread and read health (a child has read nothing yet);
  registrations survive.
- The thread starts from ``baldur.init()`` and the per-worker starter, and from
  any read of a registered copy in a process without a live refresher (a
  forked child, or a thread that died). No fork-source gate: a pre-fork
  server's worker started without the server hook reads as the fork source for
  its whole life, and the fork source itself runs scheduled jobs that read the
  copy.
- A process that never loaded a copy (a script that never called ``init()``)
  lets its first reader run one synchronous pass, without waiting when another
  pass is in flight.
- A hung tick is not replaced: every step of a pass is time-bounded by the
  store client, and a hang inside the store would hang a replacement too. The
  copy's age and the gauge make it visible.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

import structlog

from baldur.core.process_utils import fork_repaired, fork_safe_lock
from baldur.utils.time import utc_now

if TYPE_CHECKING:
    from baldur.core.state_backend import StateBackend
    from baldur.meta.daemon_worker import DaemonWorkerHandle

logger = structlog.get_logger()

__all__ = [
    "ControlStateHealth",
    "ControlStateRefresher",
    "get_control_state_refresher",
    "reset_control_state_refresher",
]

#: Registered daemon-worker name — also the thread name.
DAEMON_WORKER_NAME = "control_state_refresher"

#: Longest the thread sleeps between wake-ups, regardless of how far away the
#: nearest due key is. Bounds liveness granularity only (the handle declares it
#: as the heartbeat cadence); key timing comes from each key's own interval.
_HEARTBEAT_INTERVAL_SECONDS: float = 5.0

#: Ceiling of the retry delay after consecutive read failures. A key whose own
#: interval is longer keeps its interval.
_BACKOFF_CAP_SECONDS: float = 30.0

#: Join ceiling on ``stop()``. The wake event interrupts the sleep at once, so
#: this only trips on a pass blocked inside a store read.
_STOP_JOIN_TIMEOUT_SECONDS: float = 5.0


@dataclass(frozen=True)
class ControlStateHealth:
    """How this process's reads of one key are going.

    Attributes:
        store_reachable: ``None`` before the first attempt in this process,
            then whether the most recent read succeeded.
        refreshed_at: When this process last read the key successfully.
        age_seconds: Seconds since that read; ``None`` before one.
        last_error: The most recent read failure, while it lasts.
        consecutive_failures: Failed reads since the last success.
    """

    store_reachable: bool | None
    refreshed_at: datetime | None
    age_seconds: float | None
    last_error: str | None
    consecutive_failures: int


@dataclass
class _Registration:
    key: str
    interval: Callable[[], float]
    refresh: Callable[[StateBackend], None]


@dataclass
class _KeyHealth:
    attempted: bool = False
    reachable: bool | None = None
    refreshed_at: datetime | None = None
    refreshed_monotonic: float | None = None
    last_error: str | None = None
    consecutive_failures: int = 0
    next_due: float = 0.0


class _RefresherState:
    """Everything a ``fork()`` child must not inherit as-is, in one object.

    Held behind a single attribute so a child re-owns the whole thing with one
    store and no lock: a repair gate would be held exactly by a child
    mid-repair, and a grandchild forked in that window would inherit it held.
    """

    __slots__ = (
        "health",
        "origin_pid",
        "pass_lock",
        "spawn_failed_logged",
        "spawn_lock",
        "stopped",
        "thread",
        "wake",
    )

    def __init__(self, health: dict[str, _KeyHealth] | None = None) -> None:
        self.pass_lock = fork_safe_lock()
        self.spawn_lock = fork_safe_lock()
        self.wake = threading.Event()
        self.thread: threading.Thread | None = None
        self.health: dict[str, _KeyHealth] = health if health is not None else {}
        self.stopped = False
        self.spawn_failed_logged = False
        self.origin_pid = os.getpid()


class ControlStateRefresher:
    """Keeps each registered control-state copy within its interval of the store.

    One instance per process (``get_control_state_refresher()``), one daemon
    thread per instance.
    """

    def __init__(self) -> None:
        self._registry_lock = fork_safe_lock()
        self._registrations: dict[str, _Registration] = {}
        # Inherited across fork on purpose: a child of a process that loaded a
        # copy holds that copy, and acts on it until its own first tick.
        self._first_load_attempted = False
        self._handle: DaemonWorkerHandle | None = None
        self._state = _RefresherState()
        # Fork-repair arbiter: every thread repairing the same fork converges
        # on ONE state object, because the spawn's mutual exclusion lives on it.
        self._state_by_pid: dict[int, _RefresherState] = {}

    # =========================================================================
    # Fork re-ownership
    # =========================================================================

    def _repair_if_forked(self) -> None:
        """Re-own the lifecycle state when this process inherited it via fork.

        Lock-free on purpose — see ``_RefresherState``. The swap is arbitrated
        by one GIL-atomic ``setdefault`` so concurrent repairers land on the
        same object. Read health starts empty: the child has read nothing.
        """
        pid = os.getpid()
        if self._state.origin_pid == pid:
            return
        fresh = _RefresherState()
        owned = self._state_by_pid.setdefault(pid, fresh)
        self._state = owned
        if owned is fresh and self._handle is not None:
            self._handle.reset_after_fork()

    # =========================================================================
    # Registration
    # =========================================================================

    @fork_repaired
    def register(
        self,
        key: str,
        *,
        interval_seconds: float | Callable[[], float],
        refresh: Callable[[StateBackend], None],
    ) -> None:
        """Register (or replace) ``key``'s refresh.

        Args:
            key: The state key; also the gauge's ``key`` label.
            interval_seconds: The key's refresh interval, or a callable
                returning it (read at each scheduling decision).
            refresh: Called with the store on each pass. Performs the strict
                read and the assignment; raises when the read fails.
        """
        interval = (
            interval_seconds
            if callable(interval_seconds)
            else _constant(float(interval_seconds))
        )
        with self._registry_lock:
            self._registrations[key] = _Registration(key, interval, refresh)
        self._state.wake.set()

    @fork_repaired
    def unregister(self, key: str) -> None:
        """Drop ``key``'s registration and its read health; stop when none remain."""
        with self._registry_lock:
            self._registrations.pop(key, None)
            empty = not self._registrations
        self._state.health.pop(key, None)
        if empty:
            self.stop()

    def _snapshot_registrations(self, key: str | None = None) -> list[_Registration]:
        with self._registry_lock:
            if key is None:
                return list(self._registrations.values())
            registration = self._registrations.get(key)
            return [registration] if registration is not None else []

    # =========================================================================
    # Passes
    # =========================================================================

    @fork_repaired
    def refresh_now(self, key: str | None = None) -> None:
        """Run one synchronous pass over every registered key (or ``key``).

        Waits for a pass in flight, then reads. Used by startup and by the
        per-worker starter so a process serves its first request on a copy it
        read itself; also the deterministic seam for tests.
        """
        state = self._state
        with state.pass_lock:
            self._run_pass(state, self._snapshot_registrations(key))

    @fork_repaired
    def ensure_live(self) -> None:
        """Reader hook: load once in a never-loaded process, keep a thread alive.

        Never waits. A process that never loaded a copy runs one synchronous
        pass when it wins the pass lock; a reader that loses it uses the copy
        it has. Every other call only starts a thread when this process has
        no live one.
        """
        if not self._first_load_attempted:
            state = self._state
            if not state.pass_lock.acquire(blocking=False):
                return
            try:
                if not self._first_load_attempted:
                    self._run_pass(state, self._snapshot_registrations())
            finally:
                state.pass_lock.release()
        self._ensure_thread()

    def _run_pass(
        self, state: _RefresherState, registrations: list[_Registration]
    ) -> None:
        """Read each registration once, through one store construction attempt.

        Caller holds ``state.pass_lock``.
        """
        self._first_load_attempted = True
        if not registrations:
            return
        # def-body import: resolved per pass so a configured or patched store
        # is honored; a failed construction is not retried for the next key.
        from baldur.core.state_backend import get_state_backend

        try:
            backend: StateBackend | None = get_state_backend()
            construction_error: BaseException | None = None
        except Exception as e:
            backend = None
            construction_error = e
        for registration in registrations:
            if backend is None:
                assert construction_error is not None
                self._record_failure(state, registration, construction_error)
                continue
            try:
                registration.refresh(backend)
            except Exception as e:
                self._record_failure(state, registration, e)
                continue
            self._record_success(state, registration)

    def _key_health(self, state: _RefresherState, key: str) -> _KeyHealth:
        health = state.health.get(key)
        if health is None:
            health = state.health.setdefault(key, _KeyHealth())
        return health

    def _record_success(
        self, state: _RefresherState, registration: _Registration
    ) -> None:
        health = self._key_health(state, registration.key)
        recovered = health.consecutive_failures > 0
        now = time.monotonic()
        health.attempted = True
        health.reachable = True
        health.refreshed_at = utc_now()
        health.refreshed_monotonic = now
        health.last_error = None
        health.consecutive_failures = 0
        health.next_due = now + registration.interval()
        _set_refreshed_gauge(registration.key, health.refreshed_at.timestamp())
        if recovered:
            logger.info("control_state.refresh_recovered", state_key=registration.key)

    def _record_failure(
        self,
        state: _RefresherState,
        registration: _Registration,
        error: BaseException,
    ) -> None:
        health = self._key_health(state, registration.key)
        health.attempted = True
        health.reachable = False
        health.last_error = f"{type(error).__name__}: {error}"
        health.consecutive_failures += 1
        retry_in = _retry_delay(registration.interval(), health.consecutive_failures)
        health.next_due = time.monotonic() + retry_in
        if health.consecutive_failures == 1:
            logger.warning(
                "control_state.refresh_failed",
                state_key=registration.key,
                error=health.last_error,
                retry_in_seconds=round(retry_in, 3),
            )
        else:
            # One WARNING per failure episode: intermediate retries inside an
            # outage already reported at its edge.
            logger.debug(
                "control_state.refresh_failed",
                state_key=registration.key,
                error=health.last_error,
                consecutive_failures=health.consecutive_failures,
                retry_in_seconds=round(retry_in, 3),
            )

    # =========================================================================
    # Health
    # =========================================================================

    @fork_repaired
    def health(self, key: str) -> ControlStateHealth:
        """This process's read health for ``key`` (never inherited across fork)."""
        health = self._state.health.get(key)
        if health is None or not health.attempted:
            return ControlStateHealth(None, None, None, None, 0)
        age = (
            None
            if health.refreshed_monotonic is None
            else max(time.monotonic() - health.refreshed_monotonic, 0.0)
        )
        return ControlStateHealth(
            store_reachable=health.reachable,
            refreshed_at=health.refreshed_at,
            age_seconds=age,
            last_error=health.last_error,
            consecutive_failures=health.consecutive_failures,
        )

    @property
    @fork_repaired
    def is_running(self) -> bool:
        """Whether this process has a live refresher thread."""
        thread = self._state.thread
        return thread is not None and thread.is_alive()

    # =========================================================================
    # Thread lifecycle
    # =========================================================================

    @fork_repaired
    def start(self) -> None:
        """Start this process's refresher thread unless one is alive."""
        self._ensure_thread()

    def _ensure_thread(self) -> None:
        state = self._state
        if state.stopped:
            return
        thread = state.thread
        if thread is not None and thread.is_alive():
            return
        # Test / operator hatch: ``0`` keeps this process from starting the
        # thread; synchronous passes still run.
        autostart = os.environ.get("BALDUR_CONTROL_STATE_REFRESHER_AUTOSTART", "1")
        if autostart.strip().lower() in ("0", "false"):
            return
        try:
            self._spawn_thread()
        except RuntimeError as e:
            # Refused at interpreter shutdown and at a thread ceiling; the next
            # read retries. Reported once per process.
            if not state.spawn_failed_logged:
                state.spawn_failed_logged = True
                logger.warning("control_state.refresher_spawn_failed", error=str(e))

    def _spawn_thread(self) -> None:
        """Construct and start a fresh refresher thread (also the restart callback)."""
        self._repair_if_forked()
        state = self._state
        # The aliveness test at every entry point is a check-then-act; the
        # re-check under this lock is what makes "one thread per process" true
        # when many request threads read at once.
        with state.spawn_lock:
            if state.stopped:
                return
            running = state.thread
            if running is not None and running.is_alive():
                return
            thread = threading.Thread(
                target=self._loop_with_crash_capture,
                args=(state,),
                name=DAEMON_WORKER_NAME,
                daemon=True,
            )
            thread.start()
            state.thread = thread
            self._bind_handle(thread)
        logger.info("control_state.refresher_started")

    def _bind_handle(self, thread: threading.Thread) -> None:
        """Register this process's daemon-worker handle, or rebind it to ``thread``."""
        if self._handle is not None:
            self._handle.thread = thread
            return
        from baldur.meta.daemon_worker import DaemonWorkerHandle
        from baldur.metrics.recorders.daemon_worker import register_daemon_worker

        self._handle = DaemonWorkerHandle(
            thread=thread,
            tick_interval_seconds=_HEARTBEAT_INTERVAL_SECONDS,
            staleness_threshold_seconds=self._staleness_threshold(),
            restart_callback=self._spawn_thread,
        )
        register_daemon_worker(DAEMON_WORKER_NAME, self._handle)

    def _staleness_threshold(self) -> float:
        """Heartbeat silence that means the thread is really stuck.

        One pass legitimately pays a store read per key plus, when a key's
        copy changed, an event dispatch.
        """
        from baldur.settings.daemon_worker import get_daemon_worker_settings
        from baldur.settings.event_bus import get_event_bus_settings
        from baldur.settings.redis import get_redis_settings

        redis_settings = get_redis_settings()
        read_budget = (
            redis_settings.socket_connect_timeout + 2 * redis_settings.socket_timeout
        )
        keys = max(len(self._snapshot_registrations()), 1)
        return (
            _HEARTBEAT_INTERVAL_SECONDS
            * get_daemon_worker_settings().default_staleness_multiplier
            + keys * read_budget
            + get_event_bus_settings().handler_timeout_seconds
        )

    def _loop_with_crash_capture(self, state: _RefresherState) -> None:
        try:
            self._loop(state)
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as e:
            if self._handle is not None:
                self._handle.record_crash(e)
            raise

    def _loop(self, state: _RefresherState) -> None:
        """Refresh the due keys, heartbeat, sleep until the nearest due key."""
        while not state.stopped:
            state.wake.clear()
            started_at = time.monotonic()
            try:
                with state.pass_lock:
                    due = self._due_registrations(state, time.monotonic())
                    if due and not state.stopped:
                        self._run_pass(state, due)
            except Exception as e:
                logger.warning("control_state.pass_error", error=str(e))
            handle = self._handle
            if handle is not None:
                handle.observe_iteration(time.monotonic() - started_at)
                handle.heartbeat()
            state.wake.wait(timeout=self._next_wait(state))
        logger.info("control_state.refresher_stopped")

    def _due_registrations(
        self, state: _RefresherState, now: float
    ) -> list[_Registration]:
        due = []
        for registration in self._snapshot_registrations():
            health = state.health.get(registration.key)
            if health is None or health.next_due <= now:
                due.append(registration)
        return due

    def _next_wait(self, state: _RefresherState) -> float:
        now = time.monotonic()
        wait = _HEARTBEAT_INTERVAL_SECONDS
        for registration in self._snapshot_registrations():
            health = state.health.get(registration.key)
            next_due = 0.0 if health is None else health.next_due
            wait = min(wait, max(next_due - now, 0.0))
        return wait

    @fork_repaired
    def stop(self) -> None:
        """Stop this process's refresher thread; a later read may start a new one.

        The join is a ceiling: a pass blocked inside a store read outlives it,
        and returns to a state marked stopped. The read health carries over to
        the fresh lifecycle state.
        """
        state = self._state
        handle = self._handle
        if handle is not None:
            handle.is_stopping = True
        state.stopped = True
        state.wake.set()
        thread = state.thread
        if (
            thread is not None
            and thread.is_alive()
            and thread is not threading.current_thread()
        ):
            thread.join(timeout=_STOP_JOIN_TIMEOUT_SECONDS)
            if thread.is_alive():
                logger.warning(
                    "control_state.refresher_stop_timeout",
                    join_timeout_seconds=_STOP_JOIN_TIMEOUT_SECONDS,
                )
        with state.spawn_lock:
            state.thread = None
            if self._handle is not None:
                from baldur.metrics.recorders.daemon_worker import (
                    unregister_daemon_worker,
                )

                unregister_daemon_worker(DAEMON_WORKER_NAME)
                self._handle = None
        self._state = _RefresherState(health=state.health)

    def _reset(self) -> None:
        """Stop the thread and forget every registration and read."""
        self.stop()
        with self._registry_lock:
            self._registrations.clear()
        self._state = _RefresherState()
        self._state_by_pid.clear()
        self._first_load_attempted = False


def _constant(value: float) -> Callable[[], float]:
    return lambda: value


def _retry_delay(interval: float, consecutive_failures: int) -> float:
    """Backoff from the key's interval up to the cap (never below the interval).

    No jitter: symmetric jitter would put a failing store's retry up to its
    factor below the healthy cadence, polling it more often than a healthy one.
    Processes already retry on their own phases.
    """
    from baldur.core.backoff import ExponentialBackoff

    return ExponentialBackoff(
        base_delay=interval,
        max_delay=max(_BACKOFF_CAP_SECONDS, interval),
        jitter=False,
    ).calculate(consecutive_failures)


def _set_refreshed_gauge(key: str, timestamp: float) -> None:
    try:
        from baldur.metrics.recorders.system_control import (
            set_control_state_refreshed,
        )

        set_control_state_refreshed(key, timestamp)
    except Exception:
        pass


_refresher = ControlStateRefresher()


def get_control_state_refresher() -> ControlStateRefresher:
    """This process's control-state refresher."""
    return _refresher


def reset_control_state_refresher() -> None:
    """Stop the refresher and drop every registration and read (tests)."""
    _refresher._reset()
