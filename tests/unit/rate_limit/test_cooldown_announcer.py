"""CooldownAnnouncer unit tests — the verified all-clear.

``RATE_LIMIT_COOLDOWN_END`` used to be a prediction: a per-key timer armed for
the expiry the store returned to *this* worker, firing without reading anything
back. A peer's later 429 extends the shared cooldown through the store and
reaches this process through no event, so every worker that missed it announced
into a live cooldown and the adaptive throttle ramped outbound throughput back
into a provider that was still rate-limiting the fleet.

The announcer replaces that with a record per key plus one daemon thread that
re-reads the shared store before it announces. These cases pin the four things
that makes true:

- **What the read decides.** Every exit of the verification pass — announce,
  learn a peer's extension, skip an in-flight 429, hold on a store that cannot
  answer — is reached from the store's own reply.
- **Never early.** "Cannot tell" holds the whole process for a probe interval
  rather than reading as "ended"; a peer's extension is learned in place.
- **Never lost.** Rotation keeps one unreadable key from starving the others,
  the compare-and-delete makes the announcement exactly-once, and a lapsed or
  cleared cooldown still owes its one all-clear.
- **Lifecycle.** The thread starts on demand, is revived from the request path,
  is re-owned rather than inherited by a fork child, and on ``stop()`` gives up
  permission to announce before the join rather than by it.

The pass is driven synchronously through ``run_once()`` against a controllable
clock, so no case depends on a real sleep.
"""

from __future__ import annotations

import inspect
import os
import threading
import time
from unittest.mock import patch

import pytest
from structlog.testing import capture_logs

from baldur.core.process_utils import is_fork_source_process
from baldur.interfaces.rate_limit_storage import RateLimitState
from baldur.metrics.recorders.daemon_worker import (
    get_registered_daemon_workers,
    register_daemon_worker,
    unregister_daemon_worker,
)
from baldur.services.rate_limit_coordinator.announcer import (
    _HOLD_JITTER_RATIO,
    _STOP_JOIN_TIMEOUT_SECONDS,
    _TICK_INTERVAL_SECONDS,
    DAEMON_WORKER_NAME,
    CooldownAnnouncer,
)

KEY = "payment_api"
OTHER_KEY = "search_api"

#: Long enough that no case's arithmetic reaches it accidentally.
COOLDOWN_SECONDS = 300.0

#: Bound on any handoff between two threads inside a case. Generous relative to
#: the microseconds the handoff really takes, so it fails on a genuine block
#: rather than on scheduler noise.
HANDOFF_TIMEOUT_SECONDS = 5.0

#: Long enough for a second thread to reach a contended spawn — a signalled
#: start plus a handful of dict operations — without waiting on it forever.
CONTENTION_SETTLE_SECONDS = 0.2


# =============================================================================
# Doubles
# =============================================================================


class _Clock:
    """A wall clock the case moves, standing in for ``time.time``.

    The announcer writes its hold deadline from the injected clock and compares
    records against it, so a case that moved only the ``now`` argument of
    ``run_once`` would be comparing two different clocks.
    """

    def __init__(self, now: float | None = None) -> None:
        # Seeded from the real clock, not from a fixed epoch: the coordinator
        # writes its cooldowns against ``time.time()``, so a fixed anchor would
        # leave every record it hands over decades in the past and the cases
        # that drive one green for the wrong reason.
        self.now = time.time() if now is None else now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _Recorder:
    """The event-emission seam, recording instead of reaching the EventBus."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict, str]] = []

    def __call__(self, event_type_name, data, priority_name="HIGH"):
        self.calls.append((event_type_name, dict(data), priority_name))

    @property
    def keys(self) -> list[str]:
        return [data["key"] for _name, data, _priority in self.calls]


class _ProgrammableStore:
    """The one store method the verification pass reads.

    ``get_state_strict`` is the announcer's entire dependency on storage — the
    coordinator owns the writes — so the double implements that and nothing
    else. ``failure`` models a backend the adapter could not read; a stored
    value of ``None`` models an adapter handing back a state the comparison
    itself cannot use (a NULL column, a value some other tool wrote).
    """

    def __init__(self, values: dict[str, float | None] | None = None) -> None:
        self.values: dict[str, float | None] = dict(values or {})
        self.failure: BaseException | None = None
        self.reads: list[str] = []
        self.on_read = None

    def get_state_strict(self, key: str) -> RateLimitState:
        self.reads.append(key)
        if self.on_read is not None:
            self.on_read(key)
        if self.failure is not None:
            raise self.failure
        return RateLimitState(key=key, cooldown_until=self.values.get(key, 0.0))


#: The real class, captured before ``threadless`` swaps the module attribute —
#: the swap is global, so a case that wants two genuine threads has to say so.
_RealThread = threading.Thread


class _FakeThread:
    """A thread that is never scheduled, so lifecycle cases stay deterministic.

    The states the announcer branches on — spawned, alive, dead, outliving its
    join — are all set by the case rather than raced for, and no verification
    pass runs behind the assertions.
    """

    instances: list[_FakeThread] = []

    #: Set by the concurrency case only: the first spawn parks inside
    #: ``start()`` until released, which is the window a contender has to lose.
    start_gate: threading.Event | None = None
    entered_start: threading.Event | None = None

    def __init__(self, target=None, name=None, daemon=False) -> None:
        self.target = target
        self.name = name
        self.daemon = daemon
        self.started = False
        self.alive = False
        self.join_timeouts: list[float | None] = []
        self.survives_join = False
        _FakeThread.instances.append(self)

    def start(self) -> None:
        gate = _FakeThread.start_gate
        if gate is not None and _FakeThread.instances[0] is self:
            if _FakeThread.entered_start is not None:
                _FakeThread.entered_start.set()
            gate.wait(HANDOFF_TIMEOUT_SECONDS)
        self.started = True
        self.alive = True

    def is_alive(self) -> bool:
        return self.alive

    def join(self, timeout=None) -> None:
        self.join_timeouts.append(timeout)
        if not self.survives_join:
            self.alive = False


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def clock() -> _Clock:
    return _Clock()


@pytest.fixture
def store() -> _ProgrammableStore:
    return _ProgrammableStore()


@pytest.fixture
def recorder() -> _Recorder:
    return _Recorder()


@pytest.fixture(autouse=True)
def _drop_announcer_registration():
    """Leave no announcer handle in the process-global daemon-worker registry.

    The registry is module state that outlives the test, and a leaked handle
    pointing at a fake thread would have the liveness probe report a dead
    worker in whatever case runs next.
    """
    yield
    unregister_daemon_worker(DAEMON_WORKER_NAME)


@pytest.fixture
def announcer(store, recorder, clock, monkeypatch) -> CooldownAnnouncer:
    """An announcer whose pass the case drives, with no thread of its own.

    Every entry point that records a key also asks for the daemon thread. Left
    live it would run its own passes behind the case's assertions; the thread's
    own behavior is the lifecycle class's subject, driven there through a fake.
    """
    instance = CooldownAnnouncer(storage=store, emit=recorder, clock=clock)
    monkeypatch.setattr(instance, "_ensure_thread", lambda: None)
    return instance


@pytest.fixture
def threadless(monkeypatch) -> type[_FakeThread]:
    """Replace the thread the announcer spawns with the never-scheduled fake."""
    from baldur.services.rate_limit_coordinator import announcer as announcer_module

    _FakeThread.instances = []
    _FakeThread.start_gate = None
    _FakeThread.entered_start = None
    monkeypatch.setattr(announcer_module.threading, "Thread", _FakeThread)
    return _FakeThread


@pytest.fixture
def spawning(store, recorder, clock, threadless) -> CooldownAnnouncer:
    """An announcer that really runs its spawn path, over the fake thread."""
    return CooldownAnnouncer(storage=store, emit=recorder, clock=clock)


# =============================================================================
# Constants
# =============================================================================


class TestAnnouncerConstantsContract:
    """The shipped numbers, hardcoded."""

    def test_the_tick_interval_is_one_second(self):
        assert _TICK_INTERVAL_SECONDS == 1.0

    def test_the_hold_jitter_ratio_is_twenty_percent(self):
        assert _HOLD_JITTER_RATIO == 0.2

    def test_the_stop_join_ceiling_is_five_seconds(self):
        assert _STOP_JOIN_TIMEOUT_SECONDS == 5.0

    def test_the_daemon_worker_name_is_the_registered_label(self):
        """Also the thread name and the ``name`` label of its liveness series."""
        assert DAEMON_WORKER_NAME == "rate_limit_cooldown_announcer"


class TestAnnouncerStalenessThresholdContract:
    """A blocked read must not read as a dead worker; a hung backend must.

    The threshold is derived from the budgets one legitimate iteration can pay,
    so the shipped defaults produce two different numbers depending on whether
    the publish is an in-process call or a socket write.
    """

    @pytest.fixture(autouse=True)
    def _shipped_defaults(self, monkeypatch):
        """Read the shipped numbers, not the suite's speed knobs.

        The root conftest lowers the bus handler timeout to keep slow-handler
        tests short. This threshold is a budget assembled from that setting, so
        a case reading it through the knob would pin the test harness rather
        than what operators run.
        """
        from baldur.settings.event_bus import reset_event_bus_settings

        monkeypatch.delenv("BALDUR_EVENT_BUS_HANDLER_TIMEOUT_SECONDS", raising=False)
        reset_event_bus_settings()
        yield
        reset_event_bus_settings()

    def test_the_memory_bus_threshold_is_twenty_two_seconds(self, announcer):
        """2s of ticks + a 15s read budget + a 5s handler timeout."""
        assert announcer._staleness_threshold() == 22.0

    def test_the_redis_bus_threshold_adds_the_publish_socket_budget(
        self, announcer, monkeypatch
    ):
        """On the distributed bus the publish is a socket write on this thread."""
        from baldur.settings.event_bus import reset_event_bus_settings

        monkeypatch.setenv("BALDUR_EVENT_BUS_BACKEND", "redis")
        reset_event_bus_settings()

        assert announcer._staleness_threshold() == 32.0


# =============================================================================
# Verification pass — what the store's reply decides
# =============================================================================


class TestCooldownAnnouncerVerificationBehavior:
    """Every exit of the pass, reached from the store rather than from a timer."""

    def test_a_store_value_in_the_past_is_announced_with_the_stores_own_expiry(
        self, announcer, store, recorder, clock
    ):
        """The all-clear the whole mechanism exists to make truthful."""
        # Given a record this process learned, and a store that agrees it is over
        expiry = clock.now - 1.0
        store.values[KEY] = expiry
        announcer.track(KEY, expiry)

        # When the pass runs
        announced = announcer.run_once()

        # Then the key is announced, carrying the store's value and not the record
        assert announced == [KEY]
        name, data, priority = recorder.calls[0]
        assert name == "RATE_LIMIT_COOLDOWN_END"
        assert priority == "NORMAL"
        assert data["key"] == KEY
        assert data["cooldown_until"] == expiry
        assert data["cooldown_ended_at"] == clock.now

    def test_a_cleared_key_announces_a_cooldown_until_of_zero(
        self, announcer, store, recorder, clock
    ):
        """The store's value at announce time, so a cleared key reports no cooldown.

        The predecessor reported the expiry it had been armed for, which for an
        operator ``clear()`` is a cooldown that no longer exists anywhere.
        """
        announcer.track(KEY, clock.now - 1.0)
        store.values[KEY] = 0.0

        assert announcer.run_once() == [KEY]
        assert recorder.calls[0][1]["cooldown_until"] == 0.0

    def test_a_store_value_in_the_future_is_learned_and_not_announced(
        self, announcer, store, recorder, clock
    ):
        """The peer-extension path: the read is the only way it reaches here."""
        announcer.track(KEY, clock.now - 1.0)
        extended = clock.now + COOLDOWN_SECONDS
        store.values[KEY] = extended

        announced = announcer.run_once()

        assert announced == []
        assert recorder.calls == []
        assert announcer.pending[KEY] == extended

    def test_a_read_the_store_cannot_answer_announces_nothing(
        self, announcer, store, recorder, clock
    ):
        """ "Cannot tell" must never read as "ended" — the fold this call rejects."""
        announcer.track(KEY, clock.now - 1.0)
        store.failure = RuntimeError("backend unreachable")

        assert announcer.run_once() == []
        assert recorder.calls == []
        assert KEY in announcer.pending

    def test_a_stored_value_the_comparison_cannot_use_announces_nothing(
        self, announcer, store, recorder, clock
    ):
        """The guard spans the whole per-key step, not only the read.

        An adapter can answer with a state whose ``cooldown_until`` raises from
        the comparison rather than from the read — a NULL column, a value some
        other tool wrote. Guarding the read alone would let that escape the pass
        and kill the announcer thread.
        """
        announcer.track(KEY, clock.now - 1.0)
        store.values[KEY] = None

        assert announcer.run_once() == []
        assert recorder.calls == []
        assert KEY in announcer.pending

    def test_a_record_that_moved_during_the_read_is_not_announced(
        self, announcer, store, recorder, clock
    ):
        """The compare-and-delete, at its interleaving.

        A ``track()`` landing while the read is in flight replaces the record
        with a newer expiry. Announcing the value the pass set out with would
        release the cooldown that ``track()`` just installed.
        """
        # Given a due record whose value changes while the store is being read
        expiry = clock.now - 1.0
        announcer.track(KEY, expiry)
        store.values[KEY] = expiry
        replacement = clock.now + COOLDOWN_SECONDS
        store.on_read = lambda _key: announcer.track(KEY, replacement)

        # When the pass runs
        announced = announcer.run_once()

        # Then nothing is announced and the newer record survives
        assert announced == []
        assert recorder.calls == []
        assert announcer.pending[KEY] == replacement

    def test_the_announcement_happens_exactly_once_per_episode(
        self, announcer, store, recorder, clock
    ):
        """Repeating the pass yields one release, not one per pass.

        The record is deleted as part of the decision, so a later pass has
        nothing left to decide.
        """
        announcer.track(KEY, clock.now - 1.0)
        store.values[KEY] = clock.now - 1.0

        for _ in range(3):
            announcer.run_once()

        assert recorder.keys == [KEY]
        assert announcer.pending == {}

    def test_a_record_that_is_not_yet_due_is_not_read_at_all(
        self, announcer, store, recorder, clock
    ):
        """Negative: a live cooldown costs no store read and no announcement."""
        announcer.track(KEY, clock.now + COOLDOWN_SECONDS)

        assert announcer.run_once() == []
        assert store.reads == []
        assert recorder.calls == []

    def test_a_process_holding_no_record_announces_nothing(
        self, announcer, store, recorder
    ):
        """Record-scoped by construction: a cooldown that never cooled here.

        Announcing for a key this process never observed is an all-clear on
        behalf of workers that may still be in cooldown.
        """
        store.values[KEY] = 0.0

        assert announcer.run_once() == []
        assert store.reads == []
        assert recorder.calls == []

    def test_run_once_returns_the_keys_in_announcement_order(
        self, announcer, store, recorder, clock
    ):
        """The return value is the pass's own account of what it emitted."""
        announcer.track(OTHER_KEY, clock.now - 1.0)
        announcer.track(KEY, clock.now - 2.0)
        store.values.update({KEY: 0.0, OTHER_KEY: 0.0})

        announced = announcer.run_once()

        assert announced == [KEY, OTHER_KEY]
        assert recorder.keys == announced


# =============================================================================
# Peer extension — two workers over one store
# =============================================================================


class TestPeerExtensionBehavior:
    """The regression itself, with the writer that extends the cooldown separate
    from the worker that has to notice.

    One in-process store stands in for the shared one; the two coordinators are
    the two workers. The worker under test observes only the first 429, which is
    exactly the deployment shape a per-worker prediction gets wrong.
    """

    @pytest.fixture
    def shared_store(self):
        from baldur.adapters.rate_limit.memory_adapter import InMemoryRateLimitStorage

        return InMemoryRateLimitStorage(cleanup_interval=10_000)

    @pytest.fixture
    def make_worker(self, shared_store, recorder, clock):
        """Coordinators over the one store, each with a driveable announcer."""
        from baldur.services.rate_limit_coordinator import (
            RateLimitCoordinator,
            RateLimitCoordinatorConfig,
        )

        patches = []

        def _build():
            announcer = CooldownAnnouncer(
                storage=shared_store, emit=recorder, clock=clock
            )
            # The thread would run its own passes behind the assertions below.
            thread_patch = patch.object(announcer, "_ensure_thread", lambda: None)
            thread_patch.start()
            patches.append(thread_patch)
            instance = RateLimitCoordinator(
                storage=shared_store,
                config=RateLimitCoordinatorConfig(
                    default_retry_after=1.0,
                    backoff_multiplier=1.0,
                    jitter_percent=0.0,
                    debounce_window_seconds=0.0,
                ),
                announcer=announcer,
            )
            # Fail-open and irrelevant here; left live it probes a broker.
            broadcast_patch = patch.object(
                instance, "_broadcast_to_cluster", autospec=True
            )
            broadcast_patch.start()
            patches.append(broadcast_patch)
            return instance

        yield _build

        for started in patches:
            started.stop()

    def test_the_worker_that_missed_the_peers_429_announces_nothing(
        self, make_worker, shared_store, recorder, clock
    ):
        """The headline case: the stale armed time is not an all-clear.

        Before the verified read this is exactly where the throttle was
        released — at the expiry this worker alone had learned, while the
        shared cooldown ran on.
        """
        # Given a worker that observed one 429 and a peer that extended it
        worker = make_worker()
        peer = make_worker()
        worker.on_rate_limited(KEY, retry_after=60)
        learned = worker._announcer.pending[KEY]
        peer.on_rate_limited(KEY, retry_after=COOLDOWN_SECONDS)

        # When the moment this worker's own 429 alone would have ended arrives
        clock.advance(61.0)
        announced = worker._announcer.run_once()

        # Then nothing is announced and the peer's expiry is learned in place
        assert announced == []
        assert recorder.calls == []
        extended = shared_store.get_state(KEY).cooldown_until
        assert extended > learned
        assert worker._announcer.pending[KEY] == extended

    def test_the_learned_expiry_is_what_the_worker_finally_announces_at(
        self, make_worker, shared_store, recorder, clock
    ):
        """And the all-clear is withheld, not lost."""
        worker = make_worker()
        peer = make_worker()
        worker.on_rate_limited(KEY, retry_after=60)
        peer.on_rate_limited(KEY, retry_after=COOLDOWN_SECONDS)
        extended = shared_store.get_state(KEY).cooldown_until

        clock.advance(61.0)
        worker._announcer.run_once()
        clock.now = extended + 1.0
        announced = worker._announcer.run_once()

        assert announced == [KEY]
        assert recorder.calls[0][1]["cooldown_until"] == pytest.approx(extended)

    def test_learning_never_moves_a_record_earlier(self, announcer, store, clock):
        """The record takes the later of the two, so a lagging read cannot shorten it.

        A ``track()`` from a 429 this process is handling can land between the
        store read and the update; taking the read's value unconditionally would
        discard the newer cooldown.
        """
        announcer.track(KEY, clock.now - 1.0)
        later = clock.now + 2 * COOLDOWN_SECONDS
        store.values[KEY] = clock.now + COOLDOWN_SECONDS
        store.on_read = lambda _key: announcer._state.records.__setitem__(KEY, later)

        announcer.run_once()

        assert announcer.pending[KEY] == later


# =============================================================================
# Scan rotation — one unreadable key must not starve the rest
# =============================================================================


class TestScanRotationBehavior:
    """The pass breaks on the first key it cannot decide, so the order matters."""

    @pytest.fixture
    def hold_free_clock(self, clock, announcer):
        """Move past whatever hold the previous pass installed."""

        def _advance():
            held_until = announcer._state.held_until
            clock.now = max(clock.now, (held_until or clock.now)) + 1.0

        return _advance

    def test_a_key_the_store_cannot_answer_does_not_starve_the_next_one(
        self, announcer, store, recorder, clock, hold_free_clock
    ):
        """The counterexample rotation exists for.

        The unreadable key sorts earliest and stays that way, so an unrotated
        scan would break on it on every pass and the good key's all-clear would
        never be emitted at all.
        """
        # Given an unreadable key that is due before an announceable one
        bad, good = "broken_api", "healthy_api"
        announcer.track(bad, clock.now - 10.0)
        announcer.track(good, clock.now - 1.0)
        store.values[good] = 0.0
        store.failure = KeyError("no such column")

        # When the first pass breaks on the unreadable key
        assert announcer.run_once() == []

        # Then the next pass starts at the other key and announces it
        store.failure = None
        store.values[bad] = None
        hold_free_clock()
        assert announcer.run_once() == [good]
        assert recorder.keys == [good]
        assert bad in announcer.pending

    def test_the_scan_start_advances_by_one_key_per_pass(
        self, announcer, store, clock, hold_free_clock
    ):
        """The invariant over N passes: every key becomes first within N of them."""
        keys = ["api_a", "api_b", "api_c"]
        for offset, key in enumerate(keys):
            announcer.track(key, clock.now - 10.0 + offset)
        store.failure = RuntimeError("backend unreachable")

        for _ in range(4):
            announcer.run_once()
            hold_free_clock()

        assert store.reads == [keys[0], keys[1], keys[2], keys[0]]

    def test_a_pass_with_nothing_due_does_not_consume_a_rotation_step(
        self, announcer, store, recorder, clock
    ):
        """Negative: idle passes must not walk the scan start off the due keys.

        Advancing on an empty pass would make the starting key a function of how
        long the process had been idle, which is the opposite of the guarantee
        the rotation is there to give.
        """
        # Given an idle pass, then an unreadable key due before an announceable one
        assert announcer.run_once() == []
        bad, good = "broken_api", "healthy_api"
        announcer.track(bad, clock.now - 10.0)
        announcer.track(good, clock.now - 1.0)
        store.values[good] = 0.0
        store.failure = RuntimeError("backend unreachable")

        # When the first pass over real records runs
        announced = announcer.run_once()

        # Then it started at the unreadable key, exactly as if it were pass one
        assert announced == []
        assert recorder.calls == []
        assert store.reads == [bad]


# =============================================================================
# Store outage — the whole-process hold
# =============================================================================


class TestStoreOutageHoldBehavior:
    """While the store cannot be read, nothing in this process is announced."""

    @pytest.fixture
    def held(self, announcer, store, clock):
        """An announcer that has just entered the hold on a failing read."""
        announcer.track(KEY, clock.now - 1.0)
        store.failure = RuntimeError("backend unreachable")
        announcer.run_once()
        return announcer

    def test_a_pass_inside_the_hold_window_reads_nothing(self, held, store, clock):
        """Boundary just before the deadline: the hold is a real skip, not a retry."""
        reads_at_hold = len(store.reads)
        clock.now = held._state.held_until - 0.001

        assert held.run_once() == []
        assert len(store.reads) == reads_at_hold

    def test_a_pass_at_the_hold_deadline_reads_again(self, held, store, clock):
        """Boundary at the deadline: the hold expires rather than latching."""
        reads_at_hold = len(store.reads)
        clock.now = held._state.held_until

        held.run_once()

        assert len(store.reads) == reads_at_hold + 1

    def test_a_second_key_is_held_too_even_though_its_read_never_failed(
        self, held, store, recorder, clock
    ):
        """Whole-process, not per-key: an unreadable store answers no key.

        Pricing the other keys at one interval's delay is the deliberate trade
        against carrying two hold states.
        """
        held.track(OTHER_KEY, clock.now - 1.0)
        store.values[OTHER_KEY] = 0.0
        store.failure = None

        assert held.run_once() == []
        assert recorder.calls == []

    def test_the_retry_interval_is_drawn_once_per_outage(self, announcer, store, clock):
        """Not once per failed read — the deadline moves, the draw does not.

        Redrawing per probe would re-randomize the interval on every failure and
        pull the fleet's retries back toward lockstep at the worst moment.
        """
        announcer.track(KEY, clock.now - 1.0)
        store.failure = RuntimeError("backend unreachable")
        draws: list[tuple[float, float]] = []

        def _record_draw(*, min_delay_seconds, max_delay_seconds):
            draws.append((min_delay_seconds, max_delay_seconds))
            return max_delay_seconds

        with patch("baldur.utils.jitter.calculate_jitter", _record_draw):
            announcer.run_once()
            first_deadline = announcer._state.held_until
            clock.now = first_deadline
            announcer.run_once()

        assert len(draws) == 1
        assert announcer._state.held_until == clock.now + draws[0][1]
        assert announcer._state.held_until > first_deadline

    def test_the_retry_window_is_the_adapters_own_probe_interval(
        self, announcer, store, clock
    ):
        """The strict read attempts the real backend, so it *is* a recovery probe.

        Out-pacing the storage adapter's own gated probe would have the
        announcer hammering a backend the adapter is deliberately backing off
        from.
        """
        from baldur.settings.rate_limit import get_rate_limit_settings

        announcer.track(KEY, clock.now - 1.0)
        store.failure = RuntimeError("backend unreachable")
        drawn: list[tuple[float, float]] = []

        def _record_draw(*, min_delay_seconds, max_delay_seconds):
            drawn.append((min_delay_seconds, max_delay_seconds))
            return min_delay_seconds

        with patch("baldur.utils.jitter.calculate_jitter", _record_draw):
            announcer.run_once()

        interval = float(
            get_rate_limit_settings().redis_recovery_probe_interval_seconds
        )
        assert drawn == [
            (
                interval * (1.0 - _HOLD_JITTER_RATIO),
                interval * (1.0 + _HOLD_JITTER_RATIO),
            )
        ]

    def test_the_outage_edge_is_reported_once_at_warning(self, announcer, store, clock):
        """One WARNING per outage, so the line means "the store went away"."""
        announcer.track(KEY, clock.now - 1.0)
        store.failure = RuntimeError("backend unreachable")

        with capture_logs() as logs:
            announcer.run_once()

        edges = [
            entry
            for entry in logs
            if entry["event"] == "rate_limit_announcer.store_read_failed"
        ]
        assert [entry["log_level"] for entry in edges] == ["warning"]
        assert edges[0]["rate_limit_key"] == KEY
        assert edges[0]["retry_in_seconds"] > 0

    def test_retries_inside_the_same_outage_are_reported_at_debug(self, held, clock):
        """Intermediate retry failures were already reported at the edge."""
        clock.now = held._state.held_until

        with capture_logs() as logs:
            held.run_once()

        repeats = [
            entry
            for entry in logs
            if entry["event"] == "rate_limit_announcer.store_read_failed"
        ]
        assert [entry["log_level"] for entry in repeats] == ["debug"]

    def test_a_read_the_store_answers_clears_the_hold(
        self, held, store, recorder, clock
    ):
        """Recovery is observable, and the held keys are announced on that pass."""
        store.failure = None
        store.values[KEY] = 0.0
        clock.now = held._state.held_until

        with capture_logs() as logs:
            announced = held.run_once()

        assert announced == [KEY]
        assert recorder.keys == [KEY]
        assert held._state.held_until is None
        assert held._state.hold_delay is None
        recovered = [
            entry
            for entry in logs
            if entry["event"] == "rate_limit_announcer.store_read_recovered"
        ]
        assert [entry["log_level"] for entry in recovered] == ["info"]

    def test_a_value_the_comparison_cannot_use_does_not_clear_the_hold(
        self, announcer, store, clock
    ):
        """The read answered; the step still failed.

        Leaving the hold on the read alone would redraw the retry interval and
        re-report the edge on every pass — and announce a recovery from an
        outage that never ended — for a key no read is ever going to fix.
        """
        announcer.track(KEY, clock.now - 1.0)
        store.values[KEY] = None
        announcer.run_once()
        first_delay = announcer._state.hold_delay
        clock.now = announcer._state.held_until

        with capture_logs() as logs:
            announcer.run_once()

        assert announcer._state.hold_delay == first_delay
        assert not [
            entry
            for entry in logs
            if entry["event"] == "rate_limit_announcer.store_read_recovered"
        ]
        assert [
            entry["log_level"]
            for entry in logs
            if entry["event"] == "rate_limit_announcer.store_read_failed"
        ] == ["debug"]

    def test_a_healthy_key_verified_first_does_not_re_arm_the_outage_edge(
        self, announcer, store, clock, recorder
    ):
        """One WARNING per outage survives a pass that reads a good key first.

        The hold is a property of the pass, not of the step: the rotation
        guarantees that a healthy key eventually sorts ahead of the one the
        store cannot answer, and clearing on that step alone re-armed the edge
        for the key that is still broken — a fresh WARNING and a fresh retry
        interval every hold interval, for the whole outage.
        """
        announcer.track(KEY, clock.now - 2.0)
        announcer.track(OTHER_KEY, clock.now - 1.0)
        store.values[KEY] = None
        store.values[OTHER_KEY] = 0.0

        with capture_logs() as first_pass:
            announcer.run_once()
        clock.now = announcer._state.held_until
        with capture_logs() as second_pass:
            announced = announcer.run_once()

        # The rotation really did verify the healthy key ahead of the broken
        # one, so the case is not green for want of reaching the step.
        assert announced == [OTHER_KEY]
        assert [
            entry["log_level"]
            for entry in first_pass + second_pass
            if entry["event"] == "rate_limit_announcer.store_read_failed"
        ] == ["warning", "debug"]
        assert not [
            entry
            for entry in second_pass
            if entry["event"] == "rate_limit_announcer.store_read_recovered"
        ]

    def test_a_pass_that_answers_every_due_key_clears_the_hold(
        self, announcer, store, clock
    ):
        """Positive half: a whole pass the store answered still ends the outage."""
        announcer.track(KEY, clock.now - 2.0)
        announcer.track(OTHER_KEY, clock.now - 1.0)
        store.failure = RuntimeError("backend unreachable")
        announcer.run_once()
        store.failure = None
        store.values[KEY] = 0.0
        store.values[OTHER_KEY] = 0.0
        clock.now = announcer._state.held_until

        announcer.run_once()

        assert announcer._state.held_until is None
        assert announcer._state.hold_delay is None

    def test_a_healthy_pass_does_not_log_a_recovery_it_did_not_make(
        self, announcer, store, clock
    ):
        """Negative: the recovery line has to mean an outage actually ended."""
        announcer.track(KEY, clock.now - 1.0)
        store.values[KEY] = 0.0

        with capture_logs() as logs:
            announcer.run_once()

        assert not [
            entry
            for entry in logs
            if entry["event"] == "rate_limit_announcer.store_read_recovered"
        ]


# =============================================================================
# In-flight markers — the coordinator's store write is bracketed
# =============================================================================


class TestInFlightMarkerBehavior:
    """A 429 whose store write landed but whose record has not is not announced away."""

    def test_a_key_marked_in_flight_is_skipped_by_the_pass(
        self, announcer, store, recorder, clock
    ):
        """The window the bracket exists for.

        Between the store accepting the new cooldown and ``track()`` carrying
        it, the record still holds the pre-429 expiry the read was armed for.
        """
        announcer.track(KEY, clock.now - 1.0)
        store.values[KEY] = 0.0

        announcer.begin(KEY)
        announced = announcer.run_once()

        assert announced == []
        assert store.reads == []
        assert recorder.calls == []

    def test_the_matching_track_releases_the_key_for_the_next_pass(
        self, announcer, store, recorder, clock
    ):
        announcer.begin(KEY)
        announcer.track(KEY, clock.now - 1.0)
        store.values[KEY] = 0.0

        assert announcer.run_once() == [KEY]

    def test_a_track_carrying_no_expiry_releases_without_recording(
        self, announcer, recorder
    ):
        """The ``finally`` leg on a store that raised.

        The coordinator calls ``track`` from a ``finally``, so a failed write
        must clear the marker while recording nothing — an expiry it never
        learned would be a fabricated all-clear.
        """
        announcer.begin(KEY)
        announcer.track(KEY, None)

        assert announcer.pending == {}
        assert announcer._state.in_flight == {}
        assert recorder.calls == []

    def test_concurrent_429s_on_one_key_release_only_on_the_last_track(
        self, announcer, store, recorder, clock
    ):
        """Two request threads on one key: the marker is a count, not a flag."""
        # Given two 429s in flight for the same key over a due record
        announcer.track(KEY, clock.now - 1.0)
        store.values[KEY] = 0.0
        announcer.begin(KEY)
        announcer.begin(KEY)

        # When only one of them completes
        announcer.track(KEY, clock.now - 1.0)

        # Then the key is still shielded, and only the second track releases it
        assert announcer.run_once() == []
        announcer.track(KEY, clock.now - 1.0)
        assert announcer.run_once() == [KEY]

    def test_a_paired_bracket_leaves_no_marker_behind(self, announcer, clock):
        """A zero-valued entry would skip the key for the life of the process."""
        announcer.begin(KEY)
        announcer.track(KEY, clock.now - 1.0)

        assert KEY not in announcer._state.in_flight

    def test_another_keys_marker_does_not_shield_this_one(
        self, announcer, store, clock
    ):
        """Negative: the bracket is per key, unlike the store hold."""
        announcer.track(KEY, clock.now - 1.0)
        store.values[KEY] = 0.0
        announcer.begin(OTHER_KEY)

        assert announcer.run_once() == [KEY]

    def test_a_request_thread_is_not_blocked_by_a_slow_subscriber(
        self, announcer, store, clock
    ):
        """The emit happens after the lock is released.

        A subscriber may hold the dispatch for the bus handler timeout, and on
        the distributed bus the publish is a socket write on the announcer's own
        thread. Holding the announcer lock across either would stall every
        request thread taking a 429.
        """
        # Given a pass parked inside the emit for a due key
        announcer.track(KEY, clock.now - 1.0)
        store.values[KEY] = 0.0
        inside_emit = threading.Event()
        release_emit = threading.Event()

        def _blocking_emit(*_args, **_kwargs):
            inside_emit.set()
            release_emit.wait(timeout=HANDOFF_TIMEOUT_SECONDS)

        announcer._emit = _blocking_emit
        pass_thread = threading.Thread(target=announcer.run_once, daemon=True)
        pass_thread.start()
        assert inside_emit.wait(timeout=HANDOFF_TIMEOUT_SECONDS)

        # When a request thread marks a 429 in flight while the emit is parked
        began = threading.Event()

        def _begin() -> None:
            announcer.begin(OTHER_KEY)
            began.set()

        begin_thread = threading.Thread(target=_begin, daemon=True)
        begin_thread.start()

        # Then it returns without waiting for the subscriber
        try:
            assert began.wait(timeout=HANDOFF_TIMEOUT_SECONDS)
        finally:
            release_emit.set()
            begin_thread.join(timeout=HANDOFF_TIMEOUT_SECONDS)
            pass_thread.join(timeout=HANDOFF_TIMEOUT_SECONDS)


# =============================================================================
# Operator clear — the record is brought forward
# =============================================================================


class TestClearReverifyBehavior:
    """``clear()`` produces its all-clear instead of leaving the consumer waiting."""

    def test_a_cleared_key_this_process_recorded_is_announced_at_once(
        self, announcer, store, recorder, clock
    ):
        announcer.track(KEY, clock.now + COOLDOWN_SECONDS)
        store.values[KEY] = 0.0

        announcer.reverify(KEY)

        assert announcer.run_once() == [KEY]
        assert recorder.calls[0][1]["cooldown_until"] == 0.0

    def test_reverify_never_moves_a_record_later(self, announcer, clock):
        """It brings the next verification forward, so an earlier record survives."""
        already_due = clock.now - COOLDOWN_SECONDS
        announcer.track(KEY, already_due)

        announcer.reverify(KEY)

        assert announcer.pending[KEY] == already_due

    def test_a_key_this_process_never_recorded_stays_unknown(
        self, announcer, store, recorder
    ):
        """Negative: no record is created, so nothing is announced for it.

        Releasing here would be an all-clear for a cooldown that never cooled in
        this process — the failure the record scoping exists to avoid.
        """
        store.values[KEY] = 0.0

        announcer.reverify(KEY)

        assert announcer.pending == {}
        assert announcer.run_once() == []
        assert recorder.calls == []

    def test_reverify_asks_for_the_thread_when_a_record_is_brought_forward(
        self, spawning, clock, threadless
    ):
        """The operator escape must not wait for a thread the process lacks."""
        spawning.track(KEY, clock.now + COOLDOWN_SECONDS)
        spawning._state.thread = None

        spawning.reverify(KEY)

        assert spawning.is_alive is True

    def test_reverify_on_an_unknown_key_asks_for_no_thread(self, spawning, threadless):
        """Negative: the early return is before the spawn, not after it."""
        spawning.reverify(KEY)

        assert threadless.instances == []


# =============================================================================
# Thread lifecycle
# =============================================================================


class TestAnnouncerThreadLifecycleBehavior:
    """The thread exists exactly when this process has something to announce."""

    def test_a_process_that_never_took_a_429_runs_no_thread(self, spawning, threadless):
        """No thread, and no daemon-worker registration to explain in a dashboard."""
        spawning.ensure_running()

        assert threadless.instances == []
        assert DAEMON_WORKER_NAME not in get_registered_daemon_workers()

    def test_the_first_tracked_key_starts_the_thread(self, spawning, clock, threadless):
        spawning.track(KEY, clock.now + COOLDOWN_SECONDS)

        assert len(threadless.instances) == 1
        thread = threadless.instances[0]
        assert thread.started is True
        assert thread.daemon is True
        assert thread.name == DAEMON_WORKER_NAME

    def test_a_hookless_gunicorn_worker_still_starts_the_announcer(
        self, spawning, clock, threadless, monkeypatch
    ):
        """The fork-source predicate must not gate this spawn.

        ``is_fork_source_process()`` answers True in *every* worker of a
        deployment whose operator never wired the pre-fork server's post-fork
        hook: it reduces to "under gunicorn and not yet marked a worker", and
        the marker is set by that hook alone. The startup starters tolerate the
        false positive because the hook re-runs them; this spawn is demand-driven
        and has no such re-entry, so skipping here would leave the process with
        no announcer — and no all-clear — for its whole life, where the timer it
        replaced armed unconditionally.
        """
        monkeypatch.setenv("SERVER_SOFTWARE", "gunicorn/21.2.0")
        monkeypatch.delenv("GUNICORN_WORKER", raising=False)
        assert is_fork_source_process() is True

        spawning.track(KEY, clock.now + COOLDOWN_SECONDS)

        assert len(threadless.instances) == 1
        assert spawning.is_alive is True
        assert DAEMON_WORKER_NAME in get_registered_daemon_workers()

    def test_ensure_running_leaves_a_live_thread_alone(
        self, spawning, clock, threadless
    ):
        """Called from the request path on every wait, so it must be cheap and idempotent."""
        spawning.track(KEY, clock.now + COOLDOWN_SECONDS)

        spawning.ensure_running()
        spawning.ensure_running()

        assert len(threadless.instances) == 1

    def test_ensure_running_revives_a_dead_thread(self, spawning, clock, threadless):
        """The request path is the only call reachable during a cooldown.

        The announcer's own entry points run on a 429 or an operator clear, and
        a cooldown is precisely the window in which this process makes neither.
        """
        spawning.track(KEY, clock.now + COOLDOWN_SECONDS)
        threadless.instances[0].alive = False

        spawning.ensure_running()

        assert len(threadless.instances) == 2
        assert spawning.is_alive is True

    def test_ensure_running_after_stop_starts_nothing(
        self, spawning, clock, threadless
    ):
        """Negative: a stopped announcer stays stopped through the request path."""
        spawning.track(KEY, clock.now + COOLDOWN_SECONDS)
        spawning.stop()
        spawned_before = len(threadless.instances)

        spawning.ensure_running()

        assert len(threadless.instances) == spawned_before
        assert spawning.is_alive is False

    def test_a_refused_spawn_keeps_the_records_and_says_so(
        self, spawning, clock, threadless, monkeypatch
    ):
        """Refused at interpreter shutdown and at a live process's thread ceiling.

        Dropping the record there would disarm the all-clear silently; keeping
        it means the next entry point retries, and the WARNING is the signal
        that thread pressure is doing it.
        """
        from baldur.services.rate_limit_coordinator import announcer as announcer_module

        def _refuse(*_args, **_kwargs):
            raise RuntimeError("can't start new thread")

        monkeypatch.setattr(announcer_module.threading, "Thread", _refuse)

        with capture_logs() as logs:
            spawning.track(KEY, clock.now + COOLDOWN_SECONDS)

        assert spawning.pending[KEY] == clock.now + COOLDOWN_SECONDS
        assert spawning.is_alive is False
        refusals = [
            entry
            for entry in logs
            if entry["event"] == "rate_limit_announcer.spawn_failed"
        ]
        assert [entry["log_level"] for entry in refusals] == ["warning"]

    def test_the_handle_is_registered_once_and_rebound_per_spawn(
        self, spawning, clock, threadless
    ):
        """The registry entry and the restart callback keep pointing at one handle.

        A fresh handle per respawn would leave the liveness probe holding the
        abandoned one, reporting a worker that died every tick.
        """
        spawning.track(KEY, clock.now + COOLDOWN_SECONDS)
        handle = get_registered_daemon_workers()[DAEMON_WORKER_NAME]
        threadless.instances[0].alive = False

        spawning.ensure_running()

        assert get_registered_daemon_workers()[DAEMON_WORKER_NAME] is handle
        assert handle.thread is threadless.instances[1]

    def test_concurrent_first_429s_start_exactly_one_thread(
        self, spawning, clock, threadless
    ):
        """One loop per process, not one per request thread that raced for it.

        The aliveness test at each entry point and the assignment of the thread
        slot sit on either side of ``Thread.start()``. A 429 storm is exactly
        when several request threads reach ``track()`` together, and each one
        that read the empty slot would start a loop of its own — permanently,
        since a loop exits only on ``stop()``, and every one of them re-reads
        the shared store for every due key on every tick.
        """
        threadless.start_gate = threading.Event()
        threadless.entered_start = threading.Event()
        contender_ready = threading.Event()
        expiry = clock.now + COOLDOWN_SECONDS

        def _contend() -> None:
            contender_ready.set()
            spawning.track(OTHER_KEY, expiry)

        first = _RealThread(target=spawning.track, args=(KEY, expiry))
        second = _RealThread(target=_contend)
        try:
            first.start()
            assert threadless.entered_start.wait(HANDOFF_TIMEOUT_SECONDS)
            second.start()
            assert contender_ready.wait(HANDOFF_TIMEOUT_SECONDS)
            second.join(timeout=CONTENTION_SETTLE_SECONDS)
        finally:
            threadless.start_gate.set()
            first.join(HANDOFF_TIMEOUT_SECONDS)
            second.join(HANDOFF_TIMEOUT_SECONDS)

        assert len(threadless.instances) == 1
        assert spawning._state.thread is threadless.instances[0]
        assert spawning.pending == {KEY: expiry, OTHER_KEY: expiry}

    def test_the_registered_handle_declares_the_tick_and_the_restart_path(
        self, spawning, clock, threadless
    ):
        """A loop that slept until a one-hour expiry must not read as dead."""
        spawning.track(KEY, clock.now + COOLDOWN_SECONDS)

        handle = get_registered_daemon_workers()[DAEMON_WORKER_NAME]
        assert handle.tick_interval_seconds == _TICK_INTERVAL_SECONDS
        assert handle.staleness_threshold_seconds == spawning._staleness_threshold()
        assert handle.restart_callback == spawning._spawn_thread

    def test_the_restart_callback_repairs_a_forked_state_before_spawning(
        self, spawning, clock, threadless
    ):
        """A liveness probe can reach the spawn through an inherited handle.

        In a fork child that has run no entry point yet, the probe's respawn is
        the first thing to touch the announcer — so the repair has to live in
        the spawn rather than only in the decorated entry points.
        """
        spawning.track(KEY, clock.now + COOLDOWN_SECONDS)
        inherited = spawning._state
        inherited.origin_pid = os.getpid() + 1

        get_registered_daemon_workers()[DAEMON_WORKER_NAME].restart_callback()

        assert spawning._state is not inherited
        assert spawning._state.origin_pid == os.getpid()
        assert spawning.pending == {KEY: clock.now + COOLDOWN_SECONDS}
        assert spawning.is_alive is True


class TestAnnouncerLoopWaitBehavior:
    """The loop sleeps to the nearest deadline, clamped into the tick."""

    def test_an_idle_process_waits_the_whole_tick(self, announcer):
        assert announcer._next_wait(announcer._state) == _TICK_INTERVAL_SECONDS

    def test_a_deadline_inside_the_tick_is_slept_to_exactly(self, announcer, clock):
        announcer.track(KEY, clock.now + 0.25)

        assert announcer._next_wait(announcer._state) == pytest.approx(0.25)

    def test_a_distant_deadline_is_clamped_to_the_tick(self, announcer, clock):
        """The heartbeat cadence the registered handle declares."""
        announcer.track(KEY, clock.now + COOLDOWN_SECONDS)

        assert announcer._next_wait(announcer._state) == _TICK_INTERVAL_SECONDS

    def test_a_deadline_already_past_does_not_busy_loop(self, announcer, clock):
        """Clamping at zero matters as much as clamping at the tick.

        A key the store cannot answer keeps a past deadline for the length of
        the outage, and an unclamped negative wait would spin a core for it.
        """
        announcer.track(KEY, clock.now - COOLDOWN_SECONDS)

        assert announcer._next_wait(announcer._state) == 0.0

    def test_a_held_process_waits_the_full_tick_rather_than_spinning(
        self, announcer, store, clock
    ):
        """The hold deadline replaces the record deadline while it is in force."""
        announcer.track(KEY, clock.now - COOLDOWN_SECONDS)
        store.failure = RuntimeError("backend unreachable")
        announcer.run_once()

        assert announcer._next_wait(announcer._state) == _TICK_INTERVAL_SECONDS

    def test_an_expired_hold_with_nothing_due_waits_instead_of_spinning(
        self, announcer, store, clock
    ):
        """An expired hold is not a deadline.

        The hold is cleared by a pass that reads the store, and that pass needs
        a due record to reach. A 429 arriving during the outage moves the only
        record past the hold, so at the hold deadline nothing is due, nothing
        clears it, and preferring it anyway returned a wait of zero — a pegged
        core for as long as the new cooldown had left to run.
        """
        announcer.track(KEY, clock.now - 1.0)
        store.failure = RuntimeError("backend unreachable")
        announcer.run_once()
        held_until = announcer._state.held_until
        store.failure = None
        announcer.begin(KEY)
        announcer.track(KEY, held_until + COOLDOWN_SECONDS)
        clock.now = held_until + 0.1

        # Nothing is due, so no pass can clear the hold that is now in the past.
        assert announcer.run_once() == []
        assert store.reads == [KEY]
        assert announcer._state.held_until == held_until
        assert announcer._next_wait(announcer._state) == _TICK_INTERVAL_SECONDS

    def test_an_in_flight_key_does_not_set_the_deadline(self, announcer, clock):
        """Its record is stale by construction, so waking on it would spin.

        The pass skips an in-flight key, so a wait armed on it would return
        immediately, find nothing to do, and re-arm on the same record.
        """
        announcer.track(KEY, clock.now - COOLDOWN_SECONDS)
        announcer.begin(KEY)

        assert announcer._next_wait(announcer._state) == _TICK_INTERVAL_SECONDS

    def test_a_track_wakes_the_sleeping_loop(self, announcer, clock):
        """The record's own arrival is what cuts the sleep short."""
        assert announcer._state.wake.is_set() is False

        announcer.track(KEY, clock.now + COOLDOWN_SECONDS)

        assert announcer._state.wake.is_set() is True

    def test_the_wake_is_consumed_before_the_pass_not_after_it(
        self, announcer, clock, monkeypatch
    ):
        """Clearing after the pass would swallow a wake armed during it.

        A ``track()`` landing while the pass runs must shorten the sleep that
        follows; consuming the event afterwards would drop that record's wake
        and delay its all-clear by a whole tick.
        """
        # Given a wake already armed and a pass that stops the loop after one turn
        announcer.track(KEY, clock.now + COOLDOWN_SECONDS)
        observed: list[bool] = []

        def _one_pass():
            observed.append(announcer._state.wake.is_set())
            announcer._state.stopped = True
            return []

        monkeypatch.setattr(announcer, "run_once", _one_pass)

        # When the loop runs one iteration
        announcer._loop()

        # Then the pass saw a cleared event
        assert observed == [False]

    def test_a_pass_that_raises_does_not_kill_the_loop(self, announcer, monkeypatch):
        """The thread outliving one bad pass is what keeps later keys announceable."""
        turns = []

        def _raising_pass():
            turns.append(len(turns))
            if len(turns) >= 2:
                announcer._state.stopped = True
                return []
            raise RuntimeError("pass exploded")

        monkeypatch.setattr(announcer, "run_once", _raising_pass)

        with capture_logs() as logs:
            announcer._loop()

        assert len(turns) == 2
        failures = [
            entry
            for entry in logs
            if entry["event"] == "rate_limit_announcer.pass_failed"
        ]
        assert [entry["log_level"] for entry in failures] == ["warning"]


# =============================================================================
# Fork re-ownership
# =============================================================================


class TestForkReownBehavior:
    """A child re-owns the announcer instead of inheriting the parent's threading."""

    @pytest.fixture
    def forked(self, spawning, clock, threadless):
        """An announcer stamped as if this process had inherited it via fork."""
        spawning.track(KEY, clock.now + COOLDOWN_SECONDS)
        spawning.begin(OTHER_KEY)
        handle = get_registered_daemon_workers()[DAEMON_WORKER_NAME]
        handle.restart_count = 3
        handle.is_stopping = True
        handle.last_crash_reason = "RuntimeError: parent crash"
        spawning._state.origin_pid = os.getpid() + 1
        return spawning

    def test_a_matching_pid_leaves_the_state_untouched(
        self, spawning, clock, threadless
    ):
        """Negative: the repair must not fire in the process that built it."""
        spawning.track(KEY, clock.now + COOLDOWN_SECONDS)
        original = spawning._state

        spawning.ensure_running()

        assert spawning._state is original
        assert original.thread is threadless.instances[0]

    def test_a_pid_mismatch_swaps_the_lock_and_the_wake_event(self, forked):
        """The objects a child must not share with a process that no longer exists."""
        inherited = forked._state

        forked.ensure_running()

        assert forked._state is not inherited
        assert forked._state.lock is not inherited.lock
        assert forked._state.wake is not inherited.wake
        assert forked._state.origin_pid == os.getpid()

    def test_a_pid_mismatch_keeps_the_records(self, forked, clock):
        """A record is a wake hint, verified against the store before it is used.

        Dropping it would strand a consumer that inherited "a 429 is in force"
        from the same parent and now waits for an all-clear nothing will send.
        """
        forked.ensure_running()

        assert forked.pending == {KEY: clock.now + COOLDOWN_SECONDS}

    def test_a_pid_mismatch_drops_the_in_flight_markers(self, forked):
        """Their paired ``finally`` runs on a request thread the child does not have.

        An inherited marker is never decremented, so the key it names would be
        skipped for the life of the process.
        """
        forked.ensure_running()

        assert forked._state.in_flight == {}

    def test_a_pid_mismatch_abandons_the_parents_thread(self, forked):
        """``fork()`` copies no thread but the caller's, so the reference is dead."""
        inherited_thread = forked._state.thread

        forked._repair_if_forked()

        assert forked._state.thread is None
        assert forked._state.thread is not inherited_thread

    def test_a_pid_mismatch_resets_the_inherited_worker_statistics(self, forked):
        """The handle's identity survives, but the parent's observations do not.

        Left inherited, the child publishes the parent's respawn count and crash
        reason and computes heartbeat age from a heartbeat no thread in this
        process ever made — which reads as a worker that died.
        """
        handle = get_registered_daemon_workers()[DAEMON_WORKER_NAME]

        forked._repair_if_forked()

        assert handle.restart_count == 0
        assert handle.is_stopping is False
        assert handle.last_crash_reason is None
        assert get_registered_daemon_workers()[DAEMON_WORKER_NAME] is handle

    def test_two_threads_repairing_one_fork_share_a_single_state(
        self, spawning, clock, threadless, monkeypatch
    ):
        """Concurrent repairers must converge, because the spawn lock lives on
        the state.

        A plain store lets each racer publish its own object and then run the
        spawn against its own lock, so "one loop per process" stops holding: the
        loser's loop iterates a state no entry point can reach and no ``stop()``
        can end. The record it took is lost with it.
        """
        from baldur.services.rate_limit_coordinator import (
            announcer as announcer_module,
        )

        both_read = threading.Barrier(2, timeout=HANDOFF_TIMEOUT_SECONDS)
        real_state = announcer_module._AnnouncerState

        class _BarrieredState(real_state):
            """Holds every racer until both have read the inherited state."""

            __slots__ = ()

            def __init__(self, records=None):
                both_read.wait()
                super().__init__(records=records)

        monkeypatch.setattr(announcer_module, "_AnnouncerState", _BarrieredState)
        spawning._state.origin_pid = os.getpid() + 1
        expiry = clock.now + COOLDOWN_SECONDS

        first = _RealThread(target=spawning.track, args=(KEY, expiry))
        second = _RealThread(target=spawning.track, args=(OTHER_KEY, expiry))
        first.start()
        second.start()
        first.join(HANDOFF_TIMEOUT_SECONDS)
        second.join(HANDOFF_TIMEOUT_SECONDS)

        assert spawning.pending == {KEY: expiry, OTHER_KEY: expiry}
        assert len(threadless.instances) == 1
        assert spawning._state.thread is threadless.instances[0]

    def test_a_repeated_repair_does_not_swap_the_state_twice(self, forked):
        """The second entry point in the child must find the repair already done."""
        forked._repair_if_forked()
        repaired = forked._state

        forked._repair_if_forked()

        assert forked._state is repaired


class TestCooldownAnnouncerForkRepairCoverageContract:
    """Every public entry point reaching forked state carries the repair marker.

    A child deadlocks on the *first* acquisition, which is not necessarily the
    start path — so the coverage is asserted by introspection rather than by
    trusting that each new method remembered the decorator.
    """

    #: Written-down exemptions. Empty: every public callable on this class
    #: touches the state object a fork child must re-own.
    EXEMPT: frozenset[str] = frozenset()

    @staticmethod
    def _public_callables() -> dict[str, object]:
        found: dict[str, object] = {}
        for klass in CooldownAnnouncer.__mro__:
            if klass is object:
                continue
            for name, raw in vars(klass).items():
                if name.startswith("_") or name in found:
                    continue
                if isinstance(raw, property):
                    target = raw.fget
                elif isinstance(raw, (classmethod, staticmethod)):
                    target = raw.__func__
                elif inspect.isfunction(raw):
                    target = raw
                else:
                    continue
                if target is not None:
                    found[name] = target
        return found

    def test_the_scan_finds_the_public_surface_it_is_meant_to_check(self):
        """Guard on the guard: a scan that found nothing would pass silently."""
        assert set(self._public_callables()) == {
            "begin",
            "track",
            "reverify",
            "ensure_running",
            "run_once",
            "stop",
            "pending",
            "is_alive",
        }

    def test_every_public_callable_carries_the_fork_repair_marker(self):
        undecorated = {
            name
            for name, fn in self._public_callables().items()
            if not getattr(fn, "__fork_repaired__", False)
        }

        assert undecorated - self.EXEMPT == set()


# =============================================================================
# Stop
# =============================================================================


class TestAnnouncerStopBehavior:
    """Permission to announce is withdrawn before the join, not by it."""

    @pytest.fixture
    def isolated_registry(self):
        """Run the liveness probe over this announcer's handle alone.

        The probe walks every registered worker and respawns the dead ones, so
        a foreign handle left by another module would make this case's verdict
        depend on collection order.
        """
        existing = get_registered_daemon_workers()
        for name in existing:
            unregister_daemon_worker(name)
        yield
        for name in existing:
            unregister_daemon_worker(name)
        for name, handle in existing.items():
            register_daemon_worker(name, handle)

    def test_stop_clears_the_records_and_forbids_further_announcement(
        self, spawning, store, recorder, clock, threadless
    ):
        spawning.track(KEY, clock.now - 1.0)
        store.values[KEY] = 0.0

        spawning.stop()

        assert spawning.pending == {}
        assert spawning.run_once() == []
        assert recorder.calls == []

    def test_a_pass_returning_late_from_a_read_announces_nothing(
        self, spawning, store, recorder, clock, threadless
    ):
        """The join is a ceiling, not a guarantee.

        An iteration already blocked inside a store read outlives the stop, so
        the permission check — not the join — is what stops it announcing.
        Modelled by a record the late pass still holds after ``stop()`` cleared
        the map.
        """
        spawning.track(KEY, clock.now - 1.0)
        store.values[KEY] = 0.0
        spawning.stop()
        spawning._state.records[KEY] = clock.now - 1.0

        assert spawning._verify(KEY, clock.now - 1.0, clock.now) is None
        assert recorder.calls == []

    def test_stop_removes_the_daemon_worker_registration(
        self, spawning, clock, threadless
    ):
        """A registration outliving the thread reports a dead worker forever."""
        spawning.track(KEY, clock.now + COOLDOWN_SECONDS)
        assert DAEMON_WORKER_NAME in get_registered_daemon_workers()

        spawning.stop()

        assert DAEMON_WORKER_NAME not in get_registered_daemon_workers()
        assert spawning._handle is None
        assert spawning._state.thread is None

    def test_stop_joins_the_thread_at_the_ceiling(self, spawning, clock, threadless):
        spawning.track(KEY, clock.now + COOLDOWN_SECONDS)

        spawning.stop()

        assert threadless.instances[0].join_timeouts == [_STOP_JOIN_TIMEOUT_SECONDS]

    def test_a_thread_outliving_the_join_is_reported(self, spawning, clock, threadless):
        """Abandoning it is correct — but it is not allowed to be silent."""
        spawning.track(KEY, clock.now + COOLDOWN_SECONDS)
        threadless.instances[0].survives_join = True

        with capture_logs() as logs:
            spawning.stop()

        timeouts = [
            entry
            for entry in logs
            if entry["event"] == "daemon_worker.stop_join_timeout"
        ]
        assert [entry["log_level"] for entry in timeouts] == ["critical"]
        assert timeouts[0]["worker_name"] == DAEMON_WORKER_NAME

    def test_a_liveness_probe_running_during_the_join_reports_stopping(
        self, spawning, clock, threadless, isolated_registry
    ):
        """A graceful shutdown must not page anyone about a dead worker.

        ``is_stopping`` is set before the join precisely so a probe tick landing
        inside the join reads STOPPING rather than DEAD — and the probe would
        otherwise also try to respawn the thread that is being stopped.
        """
        from baldur.meta.health_probe import DaemonWorkerProbe

        # Given a worker whose join is where the probe tick lands
        spawning.track(KEY, clock.now + COOLDOWN_SECONDS)
        thread = threadless.instances[0]
        thread.survives_join = True
        probed = []

        def _probe_during_join(timeout=None):
            thread.join_timeouts.append(timeout)
            probed.append(DaemonWorkerProbe().probe())

        thread.join = _probe_during_join

        # When the announcer is stopped
        with capture_logs() as logs:
            spawning.stop()

        # Then the probe saw a stopping worker, not a dead one
        assert probed[0].details["workers"][DAEMON_WORKER_NAME] == {
            "status": "STOPPING"
        }
        assert not [entry for entry in logs if entry["event"] == "daemon_worker.died"]
        assert len(threadless.instances) == 1

    def test_a_spawn_landing_during_stop_leaves_no_registration_behind(
        self, spawning, clock, threadless
    ):
        """The teardown waits for a spawn that is already past its stop check.

        ``stop()`` reads the handle before the spawn publishes one, so without
        the teardown lock it unregisters nothing and the spawn then registers a
        handle onto a thread that exits at its first loop check. The liveness
        probe reports that worker dead on every tick from then on, and burns its
        respawn budget on a process that shut down cleanly.
        """
        threadless.start_gate = threading.Event()
        threadless.entered_start = threading.Event()

        spawner = _RealThread(
            target=spawning.track, args=(KEY, clock.now + COOLDOWN_SECONDS)
        )
        spawner.start()
        assert threadless.entered_start.wait(HANDOFF_TIMEOUT_SECONDS)
        stopper = _RealThread(target=spawning.stop)
        stopper.start()
        stopper.join(timeout=CONTENTION_SETTLE_SECONDS)
        threadless.start_gate.set()
        spawner.join(HANDOFF_TIMEOUT_SECONDS)
        stopper.join(HANDOFF_TIMEOUT_SECONDS)

        # The spawn really did land inside the stop, so the case is not green
        # for want of a contender.
        assert len(threadless.instances) == 1
        assert DAEMON_WORKER_NAME not in get_registered_daemon_workers()
        assert spawning._handle is None
        assert spawning._state.thread is None

    def test_stopping_an_announcer_that_never_started_is_a_no_op(
        self, spawning, threadless
    ):
        """Reached from the shutdown handler in a process that took no 429."""
        spawning.stop()

        assert threadless.instances == []
        assert spawning._handle is None


# =============================================================================
# Shutdown handler
# =============================================================================


class TestCooldownAnnouncerShutdownHandlerBehavior:
    """The announcer stops draining the shared store when shutdown begins."""

    @pytest.fixture
    def handler(self, spawning):
        from baldur.services.rate_limit_coordinator.shutdown import (
            CooldownAnnouncerShutdownHandler,
        )

        return CooldownAnnouncerShutdownHandler(announcer=spawning)

    def test_shutdown_start_stops_the_announcer(
        self, handler, spawning, clock, threadless
    ):
        spawning.track(KEY, clock.now + COOLDOWN_SECONDS)

        handler.on_shutdown_start()

        assert spawning.is_alive is False
        assert spawning.pending == {}

    def test_force_shutdown_stops_the_announcer_too(
        self, handler, spawning, clock, threadless
    ):
        """The force path is reached when the drain ceiling was not enough."""
        spawning.track(KEY, clock.now + COOLDOWN_SECONDS)

        handler.on_force_shutdown([])

        assert spawning.is_alive is False

    def test_a_live_thread_is_not_drain_complete(
        self, handler, spawning, clock, threadless
    ):
        spawning.track(KEY, clock.now + COOLDOWN_SECONDS)
        threadless.instances[0].survives_join = True

        assert handler.is_drain_complete() is False

    def test_the_drain_poll_is_non_blocking(self, handler, spawning, clock, threadless):
        """The coordinator calls this repeatedly, so each call is a poll not a wait."""
        from baldur.services.rate_limit_coordinator.shutdown import (
            _DRAIN_POLL_TIMEOUT_SECONDS,
        )

        spawning.track(KEY, clock.now + COOLDOWN_SECONDS)
        threadless.instances[0].survives_join = True

        handler.is_drain_complete()

        assert threadless.instances[0].join_timeouts == [_DRAIN_POLL_TIMEOUT_SECONDS]

    def test_a_stopped_announcer_is_drain_complete(
        self, handler, spawning, clock, threadless
    ):
        spawning.track(KEY, clock.now + COOLDOWN_SECONDS)
        handler.on_shutdown_start()

        assert handler.is_drain_complete() is True

    def test_a_process_that_never_built_a_coordinator_is_drain_complete(self):
        """And building one during shutdown to stop a thread it lacks is pure cost.

        The handler is constructed during startup, which never builds a
        coordinator — resolving one here would give every process a rate-limit
        storage backend it may never use.
        """
        from baldur.services.rate_limit_coordinator.coordinator import (
            RateLimitCoordinator,
        )
        from baldur.services.rate_limit_coordinator.shutdown import (
            CooldownAnnouncerShutdownHandler,
        )

        RateLimitCoordinator.reset_instance()
        handler = CooldownAnnouncerShutdownHandler()

        assert handler.is_drain_complete() is True
        handler.on_shutdown_start()

        assert RateLimitCoordinator._instance is None

    def test_the_handler_resolves_the_live_coordinators_announcer(
        self, spawning, clock, threadless, mock_storage
    ):
        """Built with no announcer in production, so the fallback is the real path."""
        from baldur.services.rate_limit_coordinator.coordinator import (
            RateLimitCoordinator,
        )
        from baldur.services.rate_limit_coordinator.shutdown import (
            CooldownAnnouncerShutdownHandler,
        )

        RateLimitCoordinator.reset_instance()
        instance = RateLimitCoordinator(storage=mock_storage, announcer=spawning)
        RateLimitCoordinator._instance = instance
        try:
            spawning.track(KEY, clock.now + COOLDOWN_SECONDS)

            CooldownAnnouncerShutdownHandler().on_shutdown_start()

            assert spawning.is_alive is False
        finally:
            RateLimitCoordinator.reset_instance()

    def test_the_integration_factory_builds_a_handler_without_a_coordinator(self):
        from baldur.services.rate_limit_coordinator.coordinator import (
            RateLimitCoordinator,
        )
        from baldur.services.rate_limit_coordinator.shutdown import (
            CooldownAnnouncerShutdownHandler,
            integrate_cooldown_announcer_with_shutdown_coordinator,
        )

        RateLimitCoordinator.reset_instance()

        handler = integrate_cooldown_announcer_with_shutdown_coordinator()

        assert isinstance(handler, CooldownAnnouncerShutdownHandler)
        assert RateLimitCoordinator._instance is None
