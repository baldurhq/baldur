"""RedisEventBus fork-lifecycle tests (747 D1/D3/D4/D5/D11/D12, plus D2's starter).

The dev host is Windows, so there is no real ``fork()`` here. Every node drives
the production entry points against a bus whose private state has been mutated
into the shape a fork child inherits:

    _origin_pid / _lock_pid -> another pid
    _running                -> True
    _listener_thread        -> a started-and-joined (dead) Thread
    _pubsub / _redis_client -> spies the child must never write on

That shape is exactly what the parent leaves behind, and it is what the pid
guards read, so the branches under test are the production branches. The one
property this seam cannot express — that the *parent* is undisturbed — is owned
by the Linux-only real-fork integration node.

The last class is a canary on redis-py itself: the two private behaviors the
abandon-don't-close decisions rest on. The dependency floor is open
(``redis>=4.2``), so a bump that changes either one must fail loudly here rather
than silently in a forked worker.
"""

from __future__ import annotations

import os
import socket
import threading
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest
import redis
from structlog.testing import capture_logs

from baldur.services.event_bus.redis_bus import RedisEventBus
from baldur.settings.event_bus import EventBusSettings

# The listener thread must be reachable for a join; nothing here waits on wall
# clock, so the value only bounds a hang.
_JOIN_TIMEOUT_SECONDS = 5.0

# The reconnect-branch nodes are the exception: they assert that the branch is
# *paced*, which only a clock can express. The instance override shrinks the
# 30 s production interval; the settle window is many multiples of it, so the
# spin these nodes guard against (tens of thousands of passes per second)
# overshoots the bound by orders of magnitude rather than by a scheduling
# hiccup.
_BACKOFF_SECONDS = 0.02
_SETTLE_SECONDS = 0.3


def _settle() -> None:
    """Let the listener loop run its reconnect branch a few times."""
    threading.Event().wait(_SETTLE_SECONDS)


# =============================================================================
# Fork-simulation seam
# =============================================================================


def _make_pubsub_spy() -> MagicMock:
    """A PubSub stand-in whose ``get_message`` blocks like a socket read.

    Blocking on an ``Event`` rather than returning immediately keeps the real
    listen loop off a busy spin; nothing asserts on the wait duration.
    """
    pubsub = MagicMock(spec=redis.client.PubSub, name="pubsub")
    idle = threading.Event()

    def _get_message(timeout: float | None = None, **_: object) -> None:
        idle.wait(timeout if timeout is not None else 0.01)
        return None

    pubsub.get_message.side_effect = _get_message
    return pubsub


def _make_client_spy() -> MagicMock:
    """A Redis client stand-in handing out a fresh pubsub spy per call."""
    client = MagicMock(spec=redis.Redis, name="redis_client")
    client.pubsub.side_effect = lambda *a, **kw: _make_pubsub_spy()
    return client


def _make_bus(redis_client: object | None = None) -> RedisEventBus:
    """Construct a bus with no Redis connection attempt."""
    with patch.object(RedisEventBus, "_connect_redis", return_value=False):
        bus = RedisEventBus()
    bus._redis_client = redis_client
    return bus


def _dead_thread() -> threading.Thread:
    """A Thread object Python has already marked stopped — what a fork child
    inherits in place of the parent's live listener."""
    thread = threading.Thread(target=lambda: None, name="RedisEventBusListener")
    thread.start()
    thread.join(timeout=_JOIN_TIMEOUT_SECONDS)
    assert not thread.is_alive()
    return thread


def _simulate_fork_inheritance(bus: RedisEventBus) -> MagicMock:
    """Mutate ``bus`` into the state a fork child inherits; return the pubsub spy
    the child must never touch."""
    inherited_pubsub = _make_pubsub_spy()
    bus._pubsub = inherited_pubsub
    bus._running = True
    bus._listener_thread = _dead_thread()
    bus._subscribed_redis_channels = set(bus._channels.values())
    bus._origin_pid = os.getpid() + 1
    bus._lock_pid = os.getpid() + 1
    return inherited_pubsub


@contextmanager
def _inert_listen_loop():
    """Replace the listen loop with a body that stays alive until the block ends.

    The spawn-path nodes are about what ``_spawn_listener_thread()`` does to the
    inherited state, not about what the loop then reads — but the thread has to
    stay alive for the aliveness guard to mean anything, so the stub blocks
    rather than returning.
    """
    stop = threading.Event()

    def _loop(_self: RedisEventBus) -> None:
        stop.wait(_JOIN_TIMEOUT_SECONDS)

    with patch.object(RedisEventBus, "_listen_loop", _loop):
        try:
            yield
        finally:
            stop.set()


@pytest.fixture
def stop_buses():
    """Stop every bus a test started, so no listener thread outlives the node."""
    started: list[RedisEventBus] = []
    yield started
    for bus in started:
        bus._running = False
        try:
            bus.stop_listener()
        except Exception:  # teardown must not mask the assertion failure
            pass


# =============================================================================
# D3 — _repair_if_forked()
# =============================================================================


class TestForkRepairBehavior:
    """747 D3: inherited subscription state is abandoned, never closed."""

    def test_repair_in_owning_process_changes_nothing(self):
        """Same pid → the whole call is one attribute load; no state moves."""
        bus = _make_bus(redis_client=_make_client_spy())
        pubsub = _make_pubsub_spy()
        bus._pubsub = pubsub
        bus._subscribed_redis_channels = {"baldur:events:global"}
        original_id = bus._instance_id
        original_lock = bus._lock

        bus._repair_if_forked()

        assert bus._instance_id == original_id
        assert bus._pubsub is pubsub
        assert bus._lock is original_lock
        assert bus._subscribed_redis_channels == {"baldur:events:global"}

    def test_repair_after_fork_abandons_pubsub_without_closing_it(self):
        """The inherited pubsub is dropped, not unsubscribed and not closed.

        ``unsubscribe()``/``close()`` write protocol bytes on a socket the parent
        still owns, and the Redis server would drop the PARENT's subscriptions.
        """
        # Given a bus carrying a parent's subscription state.
        bus = _make_bus(redis_client=_make_client_spy())
        inherited_pubsub = _simulate_fork_inheritance(bus)

        # When the child repairs.
        bus._repair_if_forked()

        # Then the reference is gone and nothing was written on the socket.
        assert bus._pubsub is None
        assert inherited_pubsub.method_calls == []

    def test_repair_after_fork_draws_a_fresh_instance_id(self):
        """The self-origin identity is redrawn — an inherited one makes every
        same-host sibling's message look self-sent and get dropped."""
        bus = _make_bus(redis_client=_make_client_spy())
        inherited_id = bus._instance_id
        _simulate_fork_inheritance(bus)

        bus._repair_if_forked()

        assert bus._instance_id != inherited_id
        assert len(bus._instance_id) == len(inherited_id)

    def test_repair_after_fork_keeps_the_inherited_redis_client(self):
        """The connection client is deliberately kept: redis-py's pool rebuilds
        the child's connections itself, so publishing has no gap."""
        client = _make_client_spy()
        bus = _make_bus(redis_client=client)
        _simulate_fork_inheritance(bus)

        bus._repair_if_forked()

        assert bus._redis_client is client
        assert client.close.call_count == 0

    def test_repair_after_fork_claims_the_pid_and_clears_channels(self):
        bus = _make_bus(redis_client=_make_client_spy())
        _simulate_fork_inheritance(bus)

        bus._repair_if_forked()

        assert bus._origin_pid == os.getpid()
        assert bus._lock_pid == os.getpid()
        assert bus._subscribed_redis_channels == set()

    def test_repair_leaves_running_and_handler_table_untouched(self):
        """The handler table survives fork() as copied memory; the repair owns
        neither it nor the running flag."""
        bus = _make_bus(redis_client=_make_client_spy())
        bus._handlers_registered = True
        local_bus = bus._local_bus
        _simulate_fork_inheritance(bus)

        bus._repair_if_forked()

        assert bus._running is True
        assert bus._handlers_registered is True
        assert bus._local_bus is local_bus

    def test_second_repair_in_the_same_process_is_a_noop(self):
        """Idempotent: once the pid is claimed, a repeat call redraws nothing."""
        bus = _make_bus(redis_client=_make_client_spy())
        _simulate_fork_inheritance(bus)

        bus._repair_if_forked()
        repaired_id = bus._instance_id
        repaired_lock = bus._lock

        bus._repair_if_forked()

        assert bus._instance_id == repaired_id
        assert bus._lock is repaired_lock

    def test_repair_replaces_a_lock_held_by_a_thread_that_did_not_survive(self):
        """Lock renewal is structural, not temporal.

        The inherited RLock can record an owner that does not exist in the child
        and will never release. The repair replaces it *before* any acquisition,
        gated on a pid stamp — a timeout could not tell an orphaned lock from a
        legitimately held one.
        """
        # Given an inherited lock that a live thread holds and never releases.
        bus = _make_bus(redis_client=_make_client_spy())
        _simulate_fork_inheritance(bus)
        inherited_lock = bus._lock
        acquired = threading.Event()
        release = threading.Event()

        def _hold_forever() -> None:
            with inherited_lock:
                acquired.set()
                release.wait(_JOIN_TIMEOUT_SECONDS)

        holder = threading.Thread(target=_hold_forever, daemon=True)
        holder.start()
        assert acquired.wait(timeout=_JOIN_TIMEOUT_SECONDS)

        try:
            # When the child repairs — it must not wait on that lock.
            bus._repair_if_forked()

            # Then a different lock object is installed and the repair completed.
            assert bus._lock is not inherited_lock
            assert bus._origin_pid == os.getpid()
        finally:
            release.set()
            holder.join(timeout=_JOIN_TIMEOUT_SECONDS)

    def test_repair_keeps_the_lock_when_this_process_created_it(self):
        """A matching ``_lock_pid`` means this process built the lock — replacing
        it would drop a real owner's mutual exclusion."""
        bus = _make_bus(redis_client=_make_client_spy())
        _simulate_fork_inheritance(bus)
        bus._lock_pid = os.getpid()  # lock rebuilt here, subscription still theirs
        own_lock = bus._lock

        bus._repair_if_forked()

        assert bus._lock is own_lock
        assert bus._origin_pid == os.getpid()


# =============================================================================
# D4 — start_listener() revival
# =============================================================================


class TestStartListenerForkRevivalBehavior:
    """747 D4: a fork child's dead listener is revived into a live one."""

    def test_start_listener_revives_the_dead_inherited_thread(self, stop_buses):
        """``_running=True`` plus a dead thread object must not read as running."""
        # Given a bus in the inherited shape.
        bus = _make_bus(redis_client=_make_client_spy())
        stop_buses.append(bus)
        _simulate_fork_inheritance(bus)
        inherited_thread = bus._listener_thread
        inherited_id = bus._instance_id

        # When the per-worker starter revives it.
        bus.start_listener()

        # Then a different, live thread is running under a fresh identity.
        assert bus._listener_thread is not inherited_thread
        assert bus._listener_thread is not None
        assert bus._listener_thread.is_alive()
        assert bus._instance_id != inherited_id
        assert bus._origin_pid == os.getpid()

    def test_revival_preserves_the_pre_fork_handler_table(self, stop_buses):
        """Subscriptions registered before the fork still dispatch after it."""
        from baldur.services.event_bus import EventType

        bus = _make_bus(redis_client=_make_client_spy())
        stop_buses.append(bus)

        def handler(event: object) -> None:  # pre-fork subscriber
            pass

        bus.subscribe(EventType.CONFIG_UPDATED, handler)
        subscriptions_before = len(
            bus._local_bus._subscriptions[EventType.CONFIG_UPDATED]
        )
        _simulate_fork_inheritance(bus)

        bus.start_listener()

        assert (
            len(bus._local_bus._subscriptions[EventType.CONFIG_UPDATED])
            == subscriptions_before
            == 1
        )

    def test_revival_keeps_the_redis_client_and_resubscribes_its_own_connection(
        self, stop_buses
    ):
        """The run-3 root cause: nulling the client here would manufacture a
        publish gap. The client is kept and a fresh pubsub is built on it."""
        # Given the inherited shape on a live client.
        client = _make_client_spy()
        bus = _make_bus(redis_client=client)
        stop_buses.append(bus)
        inherited_pubsub = _simulate_fork_inheritance(bus)

        # When the worker revives the listener.
        bus.start_listener()

        # Then the client is the same object and the pubsub is a new one.
        assert bus._redis_client is client
        assert bus._pubsub is not None
        assert bus._pubsub is not inherited_pubsub
        client.pubsub.assert_called_once()
        assert inherited_pubsub.method_calls == []

    def test_revival_subscribes_all_six_channels_before_logging_started(
        self, stop_buses
    ):
        """The ``listener_started`` record carries the full channel set, so a
        worker that logs the line is genuinely subscribed, not merely spawned."""
        from baldur.services.event_bus.redis_bus import BALDUR_EVENT_CHANNELS

        bus = _make_bus(redis_client=_make_client_spy())
        stop_buses.append(bus)
        _simulate_fork_inheritance(bus)

        with capture_logs() as logs:
            bus.start_listener()

        started = [e for e in logs if e["event"] == "redis_event_bus.listener_started"]
        assert len(started) == 1
        assert set(started[0]["subscribed_redis_channels"]) == set(
            BALDUR_EVENT_CHANNELS.values()
        )
        assert len(started[0]["subscribed_redis_channels"]) == 6
        assert len(bus._subscribed_redis_channels) == 6

    def test_revived_listener_reads_the_fresh_pubsub_not_the_inherited_one(
        self, stop_buses
    ):
        """The live loop polls the connection this process owns."""
        bus = _make_bus(redis_client=_make_client_spy())
        stop_buses.append(bus)
        inherited_pubsub = _simulate_fork_inheritance(bus)

        bus.start_listener()
        fresh_pubsub = bus._pubsub
        polled = threading.Event()
        # The loop polls once per tick; wait for the first read rather than a
        # fixed sleep.
        for _ in range(int(_JOIN_TIMEOUT_SECONDS * 100)):
            if fresh_pubsub.get_message.called:
                polled.set()
                break
            threading.Event().wait(0.01)

        assert polled.is_set()
        assert inherited_pubsub.get_message.call_count == 0
        assert inherited_pubsub.method_calls == []

    def test_revival_registers_the_new_thread_as_the_daemon_worker(self, stop_buses):
        """The meta-watchdog's handle must track the surviving thread, not the
        parent's dead one."""
        from baldur.metrics.recorders.daemon_worker import (
            get_registered_daemon_workers,
        )

        bus = _make_bus(redis_client=_make_client_spy())
        stop_buses.append(bus)
        _simulate_fork_inheritance(bus)

        bus.start_listener()

        handle = get_registered_daemon_workers()["RedisEventBusListener"]
        assert handle is bus._handle
        assert handle.thread is bus._listener_thread
        assert handle.thread.is_alive()


class TestStartListenerRespawnGuardBehavior:
    """747 D4: the guard consults thread aliveness, not ``_running`` alone."""

    def test_double_start_on_a_live_listener_keeps_one_thread(self, stop_buses):
        """A second call on a genuinely running listener returns early."""
        bus = _make_bus(redis_client=_make_client_spy())
        stop_buses.append(bus)

        bus.start_listener()
        first_thread = bus._listener_thread
        first_pubsub = bus._pubsub

        bus.start_listener()

        assert bus._listener_thread is first_thread
        assert bus._pubsub is first_pubsub

    def test_crashed_thread_in_the_same_process_is_respawned(self, stop_buses):
        """``_running=True`` with a dead thread is the crash shape as well as the
        fork shape — the old ``_running``-only guard left it permanently dead."""
        # Given a bus whose listener died without clearing _running.
        bus = _make_bus(redis_client=_make_client_spy())
        stop_buses.append(bus)
        bus.start_listener()
        crashed = bus._listener_thread
        bus._running = False
        crashed.join(timeout=_JOIN_TIMEOUT_SECONDS)
        assert not crashed.is_alive()
        bus._running = True  # the crash never cleared the flag

        # When the starter runs again in the same process.
        bus.start_listener()

        # Then a new live thread replaced the dead one.
        assert bus._listener_thread is not crashed
        assert bus._listener_thread.is_alive()

    def test_pubsub_setup_failure_leaves_a_live_listener_instead_of_raising(
        self, stop_buses
    ):
        """747 D12: the caller is a background-worker starter, so a raise here
        would leave ``_running=True`` with no thread and nothing that retries.

        The client is dropped alongside the pubsub: the subscribe against *this*
        client just failed, so the loop has to go back through ``_connect_redis``
        — which reports an outage by returning False, and so backs off — rather
        than retrying a subscribe that would raise straight out of the loop.
        """
        # Given a subscribe that fails at start, and a Redis still unreachable.
        bus = _make_bus(redis_client=_make_client_spy())
        stop_buses.append(bus)
        bus._RECONNECT_INTERVAL = _BACKOFF_SECONDS

        # When start_listener runs.
        with (
            patch.object(RedisEventBus, "_connect_redis", return_value=False),
            patch.object(bus, "_setup_pubsub", side_effect=RuntimeError("no route")),
            capture_logs() as logs,
        ):
            bus.start_listener()  # must not raise

            # Then the loop is alive on its own reconnect path.
            assert bus._pubsub is None
            assert bus._redis_client is None
            assert bus._listener_thread is not None
            assert bus._listener_thread.is_alive()
            _settle()

        warnings = [
            e for e in logs if e["event"] == "redis_event_bus.pubsub_setup_failed"
        ]
        assert len(warnings) == 1
        assert warnings[0]["log_level"] == "warning"

    def test_setup_failure_hands_the_loop_to_the_backing_off_reconnect_branch(
        self, stop_buses
    ):
        """The degradation only pays off if the loop survives to retry.

        With the client dropped alongside the pubsub, the branch runs the full
        ``_connect_redis`` → subscribe sequence, and an outage is reported by a
        False return that the loop answers with ``_RECONNECT_INTERVAL`` — so the
        thread neither dies on a re-raised subscribe nor spins.
        """
        # Given a listener degraded onto the reconnect branch, Redis still down.
        bus = _make_bus(redis_client=_make_client_spy())
        stop_buses.append(bus)
        bus._RECONNECT_INTERVAL = _BACKOFF_SECONDS

        with (
            patch.object(RedisEventBus, "_connect_redis", return_value=False),
            patch.object(bus, "_setup_pubsub", side_effect=RuntimeError("no route")),
            capture_logs() as logs,
        ):
            bus.start_listener()
            _settle()

        # Then the loop is still there to retry, and it was paced by the backoff
        # rather than spun. Aliveness is asserted first: a dead thread also logs
        # nothing, and would pass the pacing bound for the wrong reason.
        assert bus._listener_thread.is_alive()
        attempts = len([e for e in logs if e["event"] == "redis_event_bus.reconnected"])
        max_attempts = int(_SETTLE_SECONDS / _BACKOFF_SECONDS) + 2
        assert attempts <= max_attempts, f"{attempts} reconnect attempts — spinning"


# =============================================================================
# D5 — _spawn_listener_thread() as the watchdog restart callback
# =============================================================================


class TestSpawnListenerAtomicityBehavior:
    """747 D5: the single atomic spawn point, reached directly by the watchdog."""

    def test_respawn_on_inherited_state_never_touches_the_parents_pubsub(
        self, stop_buses
    ):
        """The watchdog bypasses ``_running`` by contract, so the repair has to
        live in the spawn path too — otherwise the respawned loop would read the
        parent's connection."""
        # Given a fork-inherited bus and an inert loop body (this node is about
        # the spawn path's repair, not about what the loop then does).
        bus = _make_bus(redis_client=_make_client_spy())
        stop_buses.append(bus)
        inherited_pubsub = _simulate_fork_inheritance(bus)

        with _inert_listen_loop():
            # When the watchdog restart callback fires.
            bus._spawn_listener_thread()

            # Then the inherited pubsub was abandoned untouched.
            assert bus._pubsub is None
            assert inherited_pubsub.method_calls == []
            assert bus._origin_pid == os.getpid()
            assert bus._listener_thread is not None
            assert bus._listener_thread.is_alive()

    def test_respawn_on_inherited_state_resubscribes_through_the_real_loop(
        self, stop_buses
    ):
        """The other half of the same claim, with the real loop body.

        Repair leaves the child holding a live client and no pubsub — a state the
        loop never saw before this document, and the only one that can reach its
        reconnect branch with a client already in hand. The branch has to
        subscribe there; reporting success without subscribing would spin it.
        """
        # Given the watchdog respawning a listener on fork-inherited state.
        bus = _make_bus(redis_client=_make_client_spy())
        stop_buses.append(bus)
        bus._RECONNECT_INTERVAL = _BACKOFF_SECONDS
        inherited_pubsub = _simulate_fork_inheritance(bus)

        # When the restart callback fires and the loop runs for real.
        with capture_logs() as logs:
            bus._spawn_listener_thread()
            _settle()

        # Then the child polls a subscription of its own, built once.
        assert bus._pubsub is not None
        assert bus._pubsub is not inherited_pubsub
        assert len(bus._subscribed_redis_channels) == 6
        assert inherited_pubsub.method_calls == []
        attempts = len([e for e in logs if e["event"] == "redis_event_bus.reconnected"])
        assert attempts == 1, f"{attempts} reconnect attempts — spinning"

    def test_respawn_racing_start_listener_leaves_exactly_one_live_thread(
        self, stop_buses
    ):
        """A watchdog restart landing on top of the per-worker starter must not
        leave two threads reading one connection.

        Repeat-run: the interleaving is scheduler-dependent, so a single pass
        proves little.
        """
        for _ in range(20):
            self._assert_one_live_thread_after_one_race(stop_buses)

    @staticmethod
    def _assert_one_live_thread_after_one_race(stop_buses) -> None:
        """One barrier-synchronized pass of the race, asserted."""
        bus = _make_bus(redis_client=_make_client_spy())
        stop_buses.append(bus)
        _simulate_fork_inheritance(bus)

        spawned: list[threading.Thread] = []
        spawned_lock = threading.Lock()
        real_thread_cls = threading.Thread

        def _recording_thread(*args: object, **kwargs: object) -> threading.Thread:
            thread = real_thread_cls(*args, **kwargs)
            with spawned_lock:
                spawned.append(thread)
            return thread

        barrier = threading.Barrier(2)

        def _start_path() -> None:
            barrier.wait(timeout=_JOIN_TIMEOUT_SECONDS)
            bus.start_listener()

        def _watchdog_path() -> None:
            barrier.wait(timeout=_JOIN_TIMEOUT_SECONDS)
            bus._spawn_listener_thread()

        with (
            _inert_listen_loop(),
            patch(
                "baldur.services.event_bus.redis_bus.threading.Thread",
                side_effect=_recording_thread,
            ),
        ):
            racers = [
                real_thread_cls(target=_start_path),
                real_thread_cls(target=_watchdog_path),
            ]
            for racer in racers:
                racer.start()
            for racer in racers:
                racer.join(timeout=_JOIN_TIMEOUT_SECONDS)
                assert not racer.is_alive()

            live = [t for t in spawned if t.is_alive()]
            assert len(live) == 1, f"{len(spawned)} spawned, {len(live)} alive"
            assert bus._listener_thread is live[0]
            if bus._handle is not None:
                assert bus._handle.thread is live[0]

        bus._running = False
        bus.stop_listener()


# =============================================================================
# D11 — stop_listener() on inherited state
# =============================================================================


class TestStopListenerForkGuardBehavior:
    """747 D11: the child abandons the parent's refs instead of unsubscribing."""

    def test_stop_on_inherited_state_never_unsubscribes_the_parent(self):
        """A child that never revived still inherits the shutdown handler and the
        coordinator's signal handlers. Its ``unsubscribe()`` would make the Redis
        server drop the PARENT from every channel — silently and permanently,
        because an unsubscribe confirmation is not an error the parent can see.
        """
        # Given a never-revived fork child.
        client = _make_client_spy()
        bus = _make_bus(redis_client=client)
        inherited_pubsub = _simulate_fork_inheritance(bus)

        # When the inherited shutdown path fires.
        bus.stop_listener()

        # Then nothing was written on the parent's socket.
        assert inherited_pubsub.method_calls == []
        assert client.close.call_count == 0

    def test_stop_on_inherited_state_abandons_every_ref(self):
        """This tears the process down rather than preparing it for use, so the
        client is nulled here (unlike in the repair path)."""
        bus = _make_bus(redis_client=_make_client_spy())
        _simulate_fork_inheritance(bus)

        bus.stop_listener()

        assert bus._running is False
        assert bus._pubsub is None
        assert bus._redis_client is None

    def test_stop_on_inherited_state_does_not_join_the_parents_thread(self):
        """The inherited thread object belongs to a process that owns it; the
        child must not block a signal frame joining it."""
        bus = _make_bus(redis_client=_make_client_spy())
        _simulate_fork_inheritance(bus)
        inherited_thread = MagicMock(spec=threading.Thread)
        bus._listener_thread = inherited_thread

        bus.stop_listener()

        inherited_thread.join.assert_not_called()

    def test_stop_in_the_owning_process_performs_the_normal_cleanup(self, stop_buses):
        """The guard must be a fork guard, not a blanket skip — a real stop still
        unsubscribes and closes."""
        # Given a listener this process started.
        bus = _make_bus(redis_client=_make_client_spy())
        stop_buses.append(bus)
        bus.start_listener()
        own_pubsub = bus._pubsub
        assert own_pubsub is not None

        # When it is stopped.
        bus.stop_listener()

        # Then the socket was torn down properly.
        own_pubsub.unsubscribe.assert_called_once()
        own_pubsub.close.assert_called_once()
        assert bus._running is False
        assert bus._pubsub is None


# =============================================================================
# D1 — the default-handler idempotence flag on the redis backend
# =============================================================================


class TestRedisBusDefaultHandlerRegistration:
    """747 D1: ``register_default_handlers()`` read a flag only the local bus
    defined, so on the redis backend the first call raised ``AttributeError``,
    bootstrap swallowed it into a warning, and every process ran with zero
    default handlers."""

    def test_redis_bus_defines_the_handlers_registered_flag(self):
        bus = _make_bus()

        assert bus._handlers_registered is False

    def test_register_default_handlers_completes_on_the_redis_backend(self, stop_buses):
        """The regression node: no AttributeError, and the flag flips."""
        from baldur.services.event_bus.bus.default_handlers import (
            register_default_handlers,
        )

        # Given the redis-backed bus as the singleton.
        bus = _make_bus(redis_client=_make_client_spy())
        stop_buses.append(bus)

        # When default-handler registration runs.
        with patch(
            "baldur.services.event_bus.bus.convenience.get_event_bus",
            return_value=bus,
        ):
            register_default_handlers()

            # Then it completed and left the guard armed.
            assert bus._handlers_registered is True
            subscriptions_after_first = sum(
                len(handlers) for handlers in bus._local_bus._subscriptions.values()
            )
            assert subscriptions_after_first > 0

            # And a second call adds nothing.
            register_default_handlers()

        assert (
            sum(len(h) for h in bus._local_bus._subscriptions.values())
            == subscriptions_after_first
        )

    def test_reset_clears_the_flag_so_handlers_re_register(self, stop_buses):
        """Symmetric with the local bus — without this a reset bus would silently
        skip re-registration."""
        bus = _make_bus(redis_client=_make_client_spy())
        stop_buses.append(bus)
        bus._handlers_registered = True

        bus.reset()

        assert bus._handlers_registered is False


# =============================================================================
# D3 — what the fresh identity buys: the self-origin filter
# =============================================================================


class TestSelfOriginFilterBehavior:
    """747 D3: the filter compares an id drawn once at construction. A fork child
    inherits it, so every same-host sibling looks self-sent."""

    @staticmethod
    def _payload_published_by(bus: RedisEventBus) -> str:
        from baldur.services.event_bus import BaldurEvent, EventType

        event = BaldurEvent(
            event_type=EventType.CONFIG_UPDATED,
            data={"key": "origin_filter"},
            source="sibling",
        )
        bus._publish_distributed(event)
        return bus._redis_client.publish.call_args[0][1]

    def test_distinct_instance_ids_deliver_the_siblings_event(self):
        """Two workers with their own identities exchange events normally."""
        # Given two buses with distinct identities.
        sender = _make_bus(redis_client=_make_client_spy())
        receiver = _make_bus(redis_client=_make_client_spy())
        assert sender._instance_id != receiver._instance_id
        payload = self._payload_published_by(sender)

        # When the receiver's listener handles the message.
        with patch.object(receiver._local_bus, "publish") as local_publish:
            receiver._handle_redis_message(payload)

        # Then it was delivered locally.
        local_publish.assert_called_once()
        assert local_publish.call_args[0][0].data == {"key": "origin_filter"}

    def test_identical_instance_ids_drop_the_siblings_event(self):
        """The pre-fix failure mode, asserted as the contrast case: an inherited
        identity makes the filter classify a sibling's message as self-sent."""
        # Given two buses sharing one identity — what fork() produced.
        sender = _make_bus(redis_client=_make_client_spy())
        receiver = _make_bus(redis_client=_make_client_spy())
        receiver._instance_id = sender._instance_id
        payload = self._payload_published_by(sender)

        # When the receiver's listener handles the message.
        with patch.object(receiver._local_bus, "publish") as local_publish:
            receiver._handle_redis_message(payload)

        # Then it was silently dropped.
        local_publish.assert_not_called()

    def test_fork_repair_restores_delivery_between_the_siblings(self):
        """Redrawing the id is what turns the dropped case back into the
        delivered one."""
        sender = _make_bus(redis_client=_make_client_spy())
        receiver = _make_bus(redis_client=_make_client_spy())
        receiver._instance_id = sender._instance_id
        _simulate_fork_inheritance(receiver)

        receiver._repair_if_forked()
        payload = self._payload_published_by(sender)

        with patch.object(receiver._local_bus, "publish") as local_publish:
            receiver._handle_redis_message(payload)

        assert receiver._instance_id != sender._instance_id
        local_publish.assert_called_once()


# =============================================================================
# D2 — the per-worker starter
# =============================================================================


class TestEventBusListenerStarterBehavior:
    """747 D2: revival is reachable per worker because it rides the
    background-worker starter registry."""

    def test_starter_is_a_registered_background_worker(self):
        from baldur import bootstrap

        assert (
            bootstrap._start_event_bus_listener_if_enabled
            in bootstrap._BACKGROUND_WORKER_STARTERS
        )

    def test_starter_skips_in_the_gunicorn_master(self):
        """The thread it would start dies at fork(); ``post_worker_init`` re-runs
        the start per worker once ``GUNICORN_WORKER=1`` flips the check."""
        from baldur import bootstrap

        with (
            patch(
                "baldur.core.process_utils.is_gunicorn_master",
                autospec=True,
                return_value=True,
            ),
            patch(
                "baldur.settings.event_bus.get_event_bus_settings", autospec=True
            ) as m_settings,
        ):
            bootstrap._start_event_bus_listener_if_enabled()

        m_settings.assert_not_called()

    def test_starter_returns_before_touching_the_singleton_on_memory_backend(self):
        """The backend defaults to ``memory``, so no AUTOSTART hatch is needed —
        but only if the gate really precedes ``get_event_bus()``. Creating the
        singleton here would build a bus in every unit-test process."""
        from baldur import bootstrap

        settings = EventBusSettings(backend="memory")

        with (
            patch(
                "baldur.core.process_utils.is_gunicorn_master",
                autospec=True,
                return_value=False,
            ),
            patch(
                "baldur.settings.event_bus.get_event_bus_settings",
                autospec=True,
                return_value=settings,
            ),
            patch(
                "baldur.services.event_bus.bus.get_event_bus", autospec=True
            ) as m_get_bus,
        ):
            bootstrap._start_event_bus_listener_if_enabled()

        m_get_bus.assert_not_called()

    def test_starter_skips_a_singleton_that_is_not_redis_backed(self):
        """``backend=redis`` but a ``configure_event_bus`` override installed a
        local bus: the protocol carries no listener surface to revive."""
        from baldur import bootstrap
        from baldur.services.event_bus.bus.event_bus import BaldurEventBus

        settings = EventBusSettings(backend="redis")
        local_bus = BaldurEventBus()

        with (
            patch(
                "baldur.core.process_utils.is_gunicorn_master",
                autospec=True,
                return_value=False,
            ),
            patch(
                "baldur.settings.event_bus.get_event_bus_settings",
                autospec=True,
                return_value=settings,
            ),
            patch(
                "baldur.services.event_bus.bus.get_event_bus",
                autospec=True,
                return_value=local_bus,
            ),
            capture_logs() as logs,
        ):
            bootstrap._start_event_bus_listener_if_enabled()  # must not raise

        assert any(
            e.get("reason") == "bus_not_redis_backed"
            for e in logs
            if e["event"] == "redis_event_bus.start_skipped"
        )

    def test_starter_starts_the_listener_on_the_redis_backend(self):
        from baldur import bootstrap

        settings = EventBusSettings(backend="redis")
        bus = _make_bus(redis_client=_make_client_spy())

        with (
            patch(
                "baldur.core.process_utils.is_gunicorn_master",
                autospec=True,
                return_value=False,
            ),
            patch(
                "baldur.settings.event_bus.get_event_bus_settings",
                autospec=True,
                return_value=settings,
            ),
            patch(
                "baldur.services.event_bus.bus.get_event_bus",
                autospec=True,
                return_value=bus,
            ),
            patch.object(bus, "start_listener", autospec=True) as m_start,
        ):
            bootstrap._start_event_bus_listener_if_enabled()
            bootstrap._start_event_bus_listener_if_enabled()

        # Idempotent at the starter level by delegation: start_listener() owns
        # the guard, so a double invocation is two harmless calls.
        assert m_start.call_count == 2

    def test_starter_swallows_a_failing_start_listener(self):
        """Fail-soft: the registry loop has no try/except of its own, so a raise
        here would abort every starter after it."""
        from baldur import bootstrap

        settings = EventBusSettings(backend="redis")
        bus = _make_bus(redis_client=_make_client_spy())

        with (
            patch(
                "baldur.core.process_utils.is_gunicorn_master",
                autospec=True,
                return_value=False,
            ),
            patch(
                "baldur.settings.event_bus.get_event_bus_settings",
                autospec=True,
                return_value=settings,
            ),
            patch(
                "baldur.services.event_bus.bus.get_event_bus",
                autospec=True,
                return_value=bus,
            ),
            patch.object(bus, "start_listener", side_effect=RuntimeError("redis down")),
            capture_logs() as logs,
        ):
            bootstrap._start_event_bus_listener_if_enabled()  # must not raise

        failures = [
            e for e in logs if e["event"] == "baldur.event_bus_listener_start_failed"
        ]
        assert len(failures) == 1
        assert failures[0]["log_level"] == "warning"


class TestBusCreationStartsListenerRegardlessOfProcessRole:
    """747 D7 was withdrawn: no master-skip was added on the creation path, so a
    hookless worker behaves exactly as it did before this change."""

    def test_create_event_bus_starts_the_listener_under_the_gunicorn_master(self):
        from baldur.services.event_bus.bus.convenience import _create_event_bus

        settings = EventBusSettings(backend="redis")

        with (
            patch(
                "baldur.settings.event_bus.get_event_bus_settings",
                autospec=True,
                return_value=settings,
            ),
            patch.object(RedisEventBus, "_connect_redis", return_value=False),
            patch.object(RedisEventBus, "start_listener", autospec=True) as m_start,
            patch(
                "baldur.core.process_utils.is_gunicorn_master",
                autospec=True,
                return_value=True,
            ),
        ):
            bus = _create_event_bus()

        assert isinstance(bus, RedisEventBus)
        m_start.assert_called_once()


# =============================================================================
# Canary — the redis-py internals the abandon-don't-close decisions rest on
# =============================================================================


class TestRedisPyForkPrimitivesContract:
    """The dependency floor is open, so a redis-py bump that changes either of
    these two private behaviors must fail here — not silently in a forked worker.

    (a) is why the repair keeps the inherited client: the pool rebuilds the
    child's connections itself, so publishing has no gap.
    (b) is why abandoning a connection is safe at all: redis-py's own teardown
    already refuses to ``shutdown()`` a socket it did not open.
    """

    def test_checkpid_resets_the_pool_without_disconnecting_pooled_connections(self):
        """``ConnectionPool._checkpid()`` on a pid mismatch drops the connection
        lists and re-stamps the pid, leaving the parent's sockets alone."""
        # Given a pool that recorded another process's pid.
        pool = redis.ConnectionPool.from_url("redis://localhost:6379/0")
        pooled = MagicMock(spec=redis.Connection, name="inherited_connection")
        pool._available_connections = [pooled]
        pool._in_use_connections = set()
        pool.pid = os.getpid() + 1

        # When any pool operation runs its fork check.
        pool._checkpid()

        # Then the lists are empty, the pid is claimed, and nothing was closed.
        assert pool._available_connections == []
        assert pool._in_use_connections == set()
        assert pool.pid == os.getpid()
        pooled.disconnect.assert_not_called()

    def test_connection_disconnect_skips_socket_shutdown_on_a_pid_mismatch(self):
        """``AbstractConnection.disconnect()`` closes the child's file descriptor
        but does not ``shutdown()`` the shared socket, which would tear down the
        parent's connection at the TCP level."""
        # Given an inherited connection holding a socket.
        conn = redis.Connection(host="localhost", port=6379)
        sock = MagicMock(spec=socket.socket, name="inherited_socket")
        conn._sock = sock
        conn.pid = os.getpid() + 1

        # When the child disconnects it.
        conn.disconnect()

        # Then only the local descriptor was released.
        sock.close.assert_called_once()
        sock.shutdown.assert_not_called()
        assert conn._sock is None

    def test_connection_disconnect_shuts_down_the_socket_it_owns(self):
        """Contrast case — the pid check is what suppresses the shutdown, so the
        owning process must still perform it."""
        conn = redis.Connection(host="localhost", port=6379)
        sock = MagicMock(spec=socket.socket, name="own_socket")
        conn._sock = sock
        conn.pid = os.getpid()

        conn.disconnect()

        sock.shutdown.assert_called_once()
        sock.close.assert_called_once()
