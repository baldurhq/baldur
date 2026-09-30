"""Per-process control-state refresher.

Target: ``baldur.core.control_state.ControlStateRefresher`` — one daemon
refresher per process that re-reads every registered control-state key on its
own interval: passes through one store construction attempt, per-key read
health (one WARNING per failure episode, INFO on recovery, a last-success
timestamp gauge), backoff from the key's interval to a cap, the reader hook
that loads a never-loaded process once without waiting, and the thread
lifecycle (one spawn under concurrent reads, a dead thread restarted on read,
no respawn while a tick is blocked, fork re-ownership, stop / reset).

Verification techniques applied (§8):
  - §8.1 Boundary analysis — the design constants; the backoff reaching and
    holding its cap, never below the key's interval
  - §8.8 State transition — read health: unknown → reachable → failing →
    recovered
  - §8.4 Side effects — WARNING once per episode, INFO on recovery, the
    gauge set only on success
  - §8.7 Concurrency — one thread under concurrent reads; a blocked tick is
    not replaced
  - §8.10 Singleton / lifecycle — fork re-own, stop, reset, unregister
  - §8.11 Time dependency — a controlled monotonic clock drives age and
    scheduling (no sleeps)
"""

from __future__ import annotations

import os
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from structlog.testing import capture_logs

from baldur.core import control_state as control_state_module
from baldur.core.control_state import (
    DAEMON_WORKER_NAME,
    ControlStateHealth,
    ControlStateRefresher,
    _retry_delay,
    get_control_state_refresher,
    reset_control_state_refresher,
)
from baldur.core.state_backend import MemoryStateBackend
from baldur.metrics.recorders.system_control import SystemControlMetricRecorder

_GET_BACKEND = "baldur.core.state_backend.get_state_backend"
_SET_GAUGE = "baldur.metrics.recorders.system_control.set_control_state_refreshed"
_JOIN_SECONDS = 5.0


class _KeyRefresh:
    """A registered key's refresh callback: records calls, raises or blocks on demand."""

    def __init__(self) -> None:
        self.backends: list[object] = []
        self.error: BaseException | None = None
        self.gate: threading.Event | None = None
        self.entered = threading.Event()

    def __call__(self, backend: object) -> None:
        self.backends.append(backend)
        self.entered.set()
        if self.gate is not None:
            self.gate.wait(_JOIN_SECONDS)
        if self.error is not None:
            raise self.error

    @property
    def calls(self) -> int:
        return len(self.backends)


class _Clock:
    """A monotonic clock the test advances by hand."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def monotonic(self) -> float:
        return self.now


@pytest.fixture
def refresher():
    """A fresh refresher per test; stopped and forgotten afterwards."""
    instance = ControlStateRefresher()
    yield instance
    instance._reset()


@pytest.fixture
def store():
    """The store every pass builds, in place of the configured one."""
    backend = MemoryStateBackend()
    with patch(_GET_BACKEND, return_value=backend):
        yield backend


@pytest.fixture
def clock():
    """A controlled monotonic clock for the refresher module only."""
    fake = _Clock()
    with patch.object(
        control_state_module, "time", SimpleNamespace(monotonic=fake.monotonic)
    ):
        yield fake


@pytest.fixture
def autostart(monkeypatch):
    """Let this test's refresher start its thread (the suite default is off)."""
    monkeypatch.setenv("BALDUR_CONTROL_STATE_REFRESHER_AUTOSTART", "1")


def _alive_refresher_threads() -> list[threading.Thread]:
    return [
        t
        for t in threading.enumerate()
        if t.name == DAEMON_WORKER_NAME and t.is_alive()
    ]


# =============================================================================
# Contract — design constants and the gauge
# =============================================================================


class TestControlStateRefresherContract:
    """Values the design names for the refresher (D5)."""

    @pytest.mark.parametrize(
        ("actual", "expected"),
        [
            (DAEMON_WORKER_NAME, "control_state_refresher"),
            (control_state_module._BACKOFF_CAP_SECONDS, 30.0),
            (control_state_module._HEARTBEAT_INTERVAL_SECONDS, 5.0),
        ],
        ids=["daemon_worker_name", "backoff_cap_seconds", "heartbeat_seconds"],
    )
    def test_refresher_design_constants(self, actual, expected):
        """Worker name, 30 s backoff cap, 5 s heartbeat cadence."""
        assert actual == expected

    def test_refreshed_gauge_is_a_per_key_last_success_timestamp(self):
        """``baldur_control_state_refreshed_timestamp_seconds{key}``."""
        gauge = SystemControlMetricRecorder()._control_state_refreshed

        assert gauge._name == "baldur_control_state_refreshed_timestamp_seconds"
        assert gauge._labelnames == ("key",)


# =============================================================================
# Behavior — passes and read health
# =============================================================================


class TestControlStateRefresherPassBehavior:
    """One pass reads every key once through one store construction attempt."""

    def test_health_before_any_attempt_is_unknown(self, refresher):
        """``store_reachable`` is ``None`` until this process tried once."""
        refresher.register("k", interval_seconds=5.0, refresh=_KeyRefresh())

        assert refresher.health("k") == ControlStateHealth(None, None, None, None, 0)

    def test_successful_pass_records_a_reachable_fresh_read(
        self, refresher, store, clock
    ):
        """After a read: reachable, refreshed now, no error, no failures."""
        refresher.register("k", interval_seconds=5.0, refresh=_KeyRefresh())

        refresher.refresh_now()

        health = refresher.health("k")
        assert health.store_reachable is True
        assert health.refreshed_at is not None
        assert health.age_seconds == 0.0
        assert health.last_error is None
        assert health.consecutive_failures == 0

    def test_pass_builds_the_store_once_and_hands_it_to_every_key(self, refresher):
        """Both keys receive the one store this pass built."""
        # Given
        backend = MemoryStateBackend()
        first, second = _KeyRefresh(), _KeyRefresh()
        refresher.register("a", interval_seconds=5.0, refresh=first)
        refresher.register("b", interval_seconds=30.0, refresh=second)

        # When
        with patch(_GET_BACKEND, return_value=backend) as get_backend:
            refresher.refresh_now()

        # Then
        get_backend.assert_called_once_with()
        assert first.backends == [backend]
        assert second.backends == [backend]

    def test_failed_construction_is_not_retried_for_the_next_key(self, refresher):
        """One construction attempt per pass: a blackholed store costs one connect."""
        # Given
        first, second = _KeyRefresh(), _KeyRefresh()
        refresher.register("a", interval_seconds=5.0, refresh=first)
        refresher.register("b", interval_seconds=5.0, refresh=second)

        # When
        with patch(
            _GET_BACKEND, side_effect=ConnectionError("store unreachable")
        ) as get_backend:
            refresher.refresh_now()

        # Then
        assert get_backend.call_count == 1
        assert first.calls == second.calls == 0
        for key in ("a", "b"):
            health = refresher.health(key)
            assert health.store_reachable is False
            assert health.last_error == "ConnectionError: store unreachable"

    def test_one_key_failing_leaves_the_other_key_healthy(self, refresher, store):
        """Read health is per key."""
        failing, healthy = _KeyRefresh(), _KeyRefresh()
        failing.error = ValueError("unparseable value")
        refresher.register("bad", interval_seconds=5.0, refresh=failing)
        refresher.register("good", interval_seconds=5.0, refresh=healthy)

        refresher.refresh_now()

        assert refresher.health("bad").store_reachable is False
        assert refresher.health("bad").consecutive_failures == 1
        assert refresher.health("good").store_reachable is True

    def test_refresh_now_with_a_key_reads_only_that_key(self, refresher, store):
        """A named pass leaves the other registrations alone."""
        named, other = _KeyRefresh(), _KeyRefresh()
        refresher.register("named", interval_seconds=5.0, refresh=named)
        refresher.register("other", interval_seconds=5.0, refresh=other)

        refresher.refresh_now("named")

        assert (named.calls, other.calls) == (1, 0)

    def test_failure_episode_logs_one_warning_then_debug_then_info_on_recovery(
        self, refresher, store
    ):
        """One WARNING per episode; the retries inside it log at DEBUG."""
        # Given
        key_refresh = _KeyRefresh()
        refresher.register("k", interval_seconds=5.0, refresh=key_refresh)

        # When: three failing passes, one success, one new failure
        with capture_logs() as logs:
            key_refresh.error = ConnectionError("down")
            for _ in range(3):
                refresher.refresh_now()
            key_refresh.error = None
            refresher.refresh_now()
            key_refresh.error = ConnectionError("down again")
            refresher.refresh_now()

        # Then
        trail = [
            (log["event"], log["log_level"])
            for log in logs
            if log["event"].startswith("control_state.refresh")
        ]
        assert trail == [
            ("control_state.refresh_failed", "warning"),
            ("control_state.refresh_failed", "debug"),
            ("control_state.refresh_failed", "debug"),
            ("control_state.refresh_recovered", "info"),
            ("control_state.refresh_failed", "warning"),
        ]

    def test_gauge_is_set_only_on_a_successful_read(self, refresher, store):
        """A failed read never refreshes the last-success timestamp."""
        # Given
        key_refresh = _KeyRefresh()
        key_refresh.error = ConnectionError("down")
        refresher.register("k", interval_seconds=5.0, refresh=key_refresh)

        with patch(_SET_GAUGE) as set_gauge:
            # When: a failed pass, then a successful one
            refresher.refresh_now()
            assert set_gauge.call_count == 0
            key_refresh.error = None
            refresher.refresh_now()

        # Then
        refreshed_at = refresher.health("k").refreshed_at
        set_gauge.assert_called_once_with("k", refreshed_at.timestamp())

    def test_gauge_failure_does_not_fail_the_pass(self, refresher, store):
        """The gauge is best-effort: a metrics failure leaves the read healthy."""
        refresher.register("k", interval_seconds=5.0, refresh=_KeyRefresh())

        with patch(_SET_GAUGE, side_effect=RuntimeError("registry down")) as gauge:
            refresher.refresh_now()

        assert gauge.call_count == 1
        assert refresher.health("k").store_reachable is True

    def test_age_counts_from_the_last_success_through_failures(
        self, refresher, store, clock
    ):
        """A stalled or failing refresher cannot report a fresh age."""
        # Given: a success at t=1000, then a failure at t=1012.5
        key_refresh = _KeyRefresh()
        refresher.register("k", interval_seconds=5.0, refresh=key_refresh)
        refresher.refresh_now()
        clock.now += 12.5
        key_refresh.error = ConnectionError("down")
        refresher.refresh_now()

        # When
        clock.now += 7.5
        health = refresher.health("k")

        # Then
        assert health.age_seconds == 20.0
        assert health.store_reachable is False


# =============================================================================
# Behavior — backoff and scheduling
# =============================================================================


class TestControlStateBackoffBehavior:
    """After consecutive failures a key backs off from its interval to the cap."""

    @pytest.mark.parametrize("failures", [1, 2, 3, 4, 5, 12], ids=lambda n: f"n{n}")
    def test_retry_delay_doubles_from_the_interval_up_to_the_cap(self, failures):
        """5 s key: 5, 10, 20, then held at the cap."""
        interval = 5.0
        cap = control_state_module._BACKOFF_CAP_SECONDS

        expected = min(interval * 2 ** (failures - 1), cap)

        assert _retry_delay(interval, failures) == expected

    @pytest.mark.parametrize("interval", [5.0, 30.0, 300.0], ids=["5s", "30s", "300s"])
    @pytest.mark.parametrize("failures", [1, 2, 6], ids=["n1", "n2", "n6"])
    def test_retry_delay_is_never_below_the_key_interval(self, interval, failures):
        """A failing store is never polled more often than a healthy one."""
        assert _retry_delay(interval, failures) >= interval

    def test_retry_delay_keeps_an_interval_longer_than_the_cap(self):
        """The emergency key at its 300 s maximum keeps 300 s, not the 30 s cap."""
        assert _retry_delay(300.0, 1) == 300.0
        assert _retry_delay(300.0, 9) == 300.0

    def test_failed_key_is_not_due_before_its_backoff_delay(
        self, refresher, store, clock
    ):
        """The next read of a failing key waits the backoff, not the interval."""
        # Given: two consecutive failures of a 5 s key
        key_refresh = _KeyRefresh()
        key_refresh.error = ConnectionError("down")
        refresher.register("k", interval_seconds=5.0, refresh=key_refresh)
        refresher.refresh_now()
        refresher.refresh_now()
        delay = _retry_delay(5.0, 2)
        state = refresher._state

        # When / Then
        assert refresher._due_registrations(state, clock.now + delay - 0.001) == []
        assert [
            r.key for r in refresher._due_registrations(state, clock.now + delay)
        ] == ["k"]


class TestControlStateSchedulingBehavior:
    """The thread wakes on the nearest due key, at least every heartbeat."""

    def test_never_read_key_is_due_at_once(self, refresher, clock):
        """A key registered and not yet read makes the next wait zero."""
        refresher.register("k", interval_seconds=5.0, refresh=_KeyRefresh())

        assert refresher._next_wait(refresher._state) == 0.0

    def test_wait_is_the_nearest_due_key_capped_at_the_heartbeat(
        self, refresher, store, clock
    ):
        """Two keys read now: the wait is the shorter interval, never over 5 s."""
        refresher.register("fast", interval_seconds=2.0, refresh=_KeyRefresh())
        refresher.register("slow", interval_seconds=30.0, refresh=_KeyRefresh())
        refresher.refresh_now()

        assert refresher._next_wait(refresher._state) == 2.0
        refresher.unregister("fast")
        assert refresher._next_wait(refresher._state) == (
            control_state_module._HEARTBEAT_INTERVAL_SECONDS
        )

    def test_callable_interval_is_read_at_each_scheduling_decision(
        self, refresher, store, clock
    ):
        """A changed interval (settings reload) takes effect at the next read."""
        interval = {"seconds": 30.0}
        refresher.register(
            "k", interval_seconds=lambda: interval["seconds"], refresh=_KeyRefresh()
        )
        interval["seconds"] = 4.0

        refresher.refresh_now()

        assert refresher._next_wait(refresher._state) == 4.0


# =============================================================================
# Behavior — the reader hook
# =============================================================================


class TestControlStateEnsureLiveBehavior:
    """A read loads a never-loaded process once, never waits, keeps a thread alive."""

    def test_first_read_in_a_never_loaded_process_runs_one_synchronous_pass(
        self, refresher, store
    ):
        """Only the first read loads; later reads use the copy."""
        key_refresh = _KeyRefresh()
        refresher.register("k", interval_seconds=5.0, refresh=key_refresh)

        refresher.ensure_live()
        refresher.ensure_live()

        assert key_refresh.calls == 1

    def test_read_while_a_pass_is_in_flight_does_not_wait_or_load(
        self, refresher, store
    ):
        """A reader that loses the pass lock returns at once; a later read loads."""
        # Given: a pass holds the lock
        key_refresh = _KeyRefresh()
        refresher.register("k", interval_seconds=5.0, refresh=key_refresh)
        refresher._state.pass_lock.acquire()

        # When
        try:
            refresher.ensure_live()
            calls_while_held = key_refresh.calls
        finally:
            refresher._state.pass_lock.release()
        refresher.ensure_live()

        # Then
        assert calls_while_held == 0
        assert key_refresh.calls == 1

    def test_failed_first_load_is_not_repeated_on_the_next_read(self, refresher, store):
        """A failed load counts as the attempt: the refresher, not readers, retries."""
        key_refresh = _KeyRefresh()
        key_refresh.error = ConnectionError("down")
        refresher.register("k", interval_seconds=5.0, refresh=key_refresh)

        refresher.ensure_live()
        refresher.ensure_live()

        assert key_refresh.calls == 1
        assert refresher.health("k").store_reachable is False

    @pytest.mark.parametrize(
        "value", ["0", "false", " False "], ids=["0", "false", "padded"]
    )
    def test_autostart_hatch_keeps_the_thread_off(
        self, refresher, store, monkeypatch, value
    ):
        """The hatch stops the thread; the synchronous load still runs."""
        monkeypatch.setenv("BALDUR_CONTROL_STATE_REFRESHER_AUTOSTART", value)
        key_refresh = _KeyRefresh()
        refresher.register("k", interval_seconds=5.0, refresh=key_refresh)

        refresher.ensure_live()

        assert refresher.is_running is False
        assert key_refresh.calls == 1

    def test_read_with_no_live_thread_starts_one(self, refresher, store, autostart):
        """With the hatch open, a read in a process without a thread starts it."""
        refresher.register("k", interval_seconds=60.0, refresh=_KeyRefresh())

        refresher.ensure_live()

        assert refresher.is_running is True
        assert refresher._state.thread.name == DAEMON_WORKER_NAME


# =============================================================================
# Behavior — thread lifecycle
# =============================================================================


class TestControlStateLifecycleBehavior:
    """One thread per process; restarted when dead; re-owned after fork."""

    def test_concurrent_reads_spawn_exactly_one_thread(
        self, refresher, store, autostart
    ):
        """The spawn lock's re-check makes "one thread per process" true."""
        # Given: a loaded process with no thread yet, and 16 readers
        refresher.register("k", interval_seconds=60.0, refresh=_KeyRefresh())
        refresher._first_load_attempted = True
        start = threading.Barrier(16)
        errors: list[BaseException] = []

        def reader() -> None:
            try:
                start.wait(_JOIN_SECONDS)
                refresher.ensure_live()
            except BaseException as e:
                errors.append(e)

        readers = [threading.Thread(target=reader) for _ in range(16)]

        # When
        for t in readers:
            t.start()
        for t in readers:
            t.join(_JOIN_SECONDS)

        # Then
        assert errors == []
        assert _alive_refresher_threads() == [refresher._state.thread]

    def test_blocked_tick_is_not_replaced_by_a_read(self, refresher, store, autostart):
        """A tick hung inside a store read is visible, never duplicated."""
        # Given: the thread's first tick blocks inside the key's read
        key_refresh = _KeyRefresh()
        key_refresh.gate = threading.Event()
        refresher.register("k", interval_seconds=60.0, refresh=key_refresh)
        refresher._first_load_attempted = True
        refresher.start()
        assert key_refresh.entered.wait(_JOIN_SECONDS)
        running = refresher._state.thread

        # When
        for _ in range(5):
            refresher.ensure_live()

        # Then
        try:
            assert _alive_refresher_threads() == [running]
        finally:
            key_refresh.gate.set()

    def test_dead_thread_is_restarted_by_the_next_read(
        self, refresher, store, autostart
    ):
        """Crash capture restarts nothing on OSS; a read does."""
        # Given: this process's refresher thread has died
        refresher.register("k", interval_seconds=60.0, refresh=_KeyRefresh())
        refresher._first_load_attempted = True
        dead = threading.Thread(target=lambda: None, name=DAEMON_WORKER_NAME)
        dead.start()
        dead.join()
        refresher._state.thread = dead

        # When
        refresher.ensure_live()

        # Then
        assert refresher._state.thread is not dead
        assert refresher.is_running is True

    def test_forked_child_reowns_state_without_inherited_health(self, refresher, store):
        """A child keeps the registrations and the copy, not the parent's read health."""
        # Given: the parent read the key successfully
        key_refresh = _KeyRefresh()
        refresher.register("k", interval_seconds=5.0, refresh=key_refresh)
        refresher.refresh_now()
        parent_state = refresher._state
        child_os = SimpleNamespace(getpid=lambda: os.getpid() + 1, environ=os.environ)

        # When: the same object is used from a process with another pid
        with patch.object(control_state_module, "os", child_os):
            child_health = refresher.health("k")
            refresher.ensure_live()
            calls_after_ensure_live = key_refresh.calls
            refresher.refresh_now()

        # Then
        assert refresher._state is not parent_state
        assert child_health == ControlStateHealth(None, None, None, None, 0)
        assert calls_after_ensure_live == 1
        assert key_refresh.calls == 2

    def test_stop_joins_the_thread_and_a_later_read_starts_a_new_one(
        self, refresher, store, autostart
    ):
        """Stop is not permanent: the next read may start a fresh thread."""
        refresher.register("k", interval_seconds=60.0, refresh=_KeyRefresh())
        refresher._first_load_attempted = True
        refresher.start()
        first = refresher._state.thread

        refresher.stop()
        stopped = refresher.is_running
        refresher.ensure_live()

        assert stopped is False
        assert not first.is_alive()
        assert refresher.is_running is True
        assert refresher._state.thread is not first

    def test_stop_keeps_the_read_health(self, refresher, store):
        """Health carries over to the fresh lifecycle state."""
        refresher.register("k", interval_seconds=5.0, refresh=_KeyRefresh())
        refresher.refresh_now()

        refresher.stop()

        assert refresher.health("k").store_reachable is True

    def test_unregistering_the_last_key_stops_the_thread(
        self, refresher, store, autostart
    ):
        """A refresher with nothing to refresh does not keep a thread."""
        refresher.register("k", interval_seconds=60.0, refresh=_KeyRefresh())
        refresher._first_load_attempted = True
        refresher.start()

        refresher.unregister("k")

        assert refresher.is_running is False
        assert refresher.health("k") == ControlStateHealth(None, None, None, None, 0)

    def test_reset_forgets_registrations_health_and_the_first_load(
        self, refresher, store
    ):
        """After a reset the next read is a first load with nothing registered."""
        key_refresh = _KeyRefresh()
        refresher.register("k", interval_seconds=5.0, refresh=key_refresh)
        refresher.refresh_now()

        refresher._reset()
        refresher.refresh_now()

        assert key_refresh.calls == 1
        assert refresher.health("k").store_reachable is None
        assert refresher._first_load_attempted is True


class TestControlStateSingletonBehavior:
    """``get_control_state_refresher`` / ``reset_control_state_refresher`` pair."""

    @pytest.fixture(autouse=True)
    def _restore_system_control_registration(self):
        yield
        # The reset below drops every registration on the process refresher;
        # a fresh manager registers the switch key again for later tests.
        from baldur.services.system_control import (
            SystemControlManager,
            reset_system_control,
        )

        SystemControlManager._instance = None
        reset_system_control(cleanup=False)

    def test_get_returns_one_refresher_per_process(self):
        """Every manager in the process registers with the same refresher."""
        assert get_control_state_refresher() is get_control_state_refresher()

    def test_reset_drops_every_registration_and_read(self, store):
        """A reset refresher refreshes nothing until keys register again."""
        # Given
        key_refresh = _KeyRefresh()
        global_refresher = get_control_state_refresher()
        global_refresher.register("test-key", interval_seconds=5.0, refresh=key_refresh)
        global_refresher.refresh_now("test-key")

        # When
        reset_control_state_refresher()
        global_refresher.refresh_now()

        # Then
        assert key_refresh.calls == 1
        assert global_refresher.health("test-key").store_reachable is None
        assert global_refresher._snapshot_registrations() == []


class TestControlStateSpawnFailureBehavior:
    """A refused thread start is reported once and retried on the next read."""

    def test_spawn_refusal_warns_once_per_process(self, refresher, store, autostart):
        """Interpreter shutdown or a thread ceiling: one WARNING, no raise."""
        refresher.register("k", interval_seconds=60.0, refresh=_KeyRefresh())
        refresher._first_load_attempted = True
        refused = MagicMock(spec=threading.Thread)
        refused.start.side_effect = RuntimeError("can't start new thread")
        module_threading = SimpleNamespace(
            Thread=MagicMock(spec=threading.Thread, return_value=refused),
            Event=threading.Event,
            current_thread=threading.current_thread,
        )

        with (
            patch.object(control_state_module, "threading", module_threading),
            capture_logs() as logs,
        ):
            refresher.ensure_live()
            refresher.ensure_live()

        assert refused.start.call_count == 2
        assert [log["event"] for log in logs].count(
            "control_state.refresher_spawn_failed"
        ) == 1
