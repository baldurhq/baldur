"""Outbox lifecycle unit tests (impl doc 486 D7, D8 — start / stop / flush / reset).

Covers Test Assessment rows:
- ``TestOutboxLifecycleBehavior`` — state_transition / idempotency
- ``TestOutboxPutContract`` — wraps with ``(enqueue_time, kwargs)`` tuple
- ``TestOutboxFromSettingsContract`` — RingBuffer constructed with DROP_OLDEST + per-feature settings
- ``TestSetupOutboxContract`` — concurrent re-entry idempotency
- ``TestResetOutboxBehavior`` — drains + stops + clears (vs just clears)
"""

from __future__ import annotations

import threading
import time
from contextlib import ExitStack
from unittest.mock import PropertyMock, patch

import pytest

from baldur.models.dlq import DLQEntryResult
from baldur.services.dlq_outbox import outbox as outbox_module
from baldur.services.dlq_outbox.outbox import (
    Outbox,
    OutboxShutdownResult,
    OutboxStats,
    flush_and_wait,
    get_outbox,
    get_shutdown_reserve_seconds,
    reset_dlq_outbox,
    setup_dlq_outbox,
    stop_outbox_for_shutdown,
)
from baldur.settings.backpressure import BackpressureStrategy

# =============================================================================
# Behavior — Outbox basic lifecycle
# =============================================================================


class TestOutboxLifecycleBehavior:
    """Start / stop / flush state transitions and idempotency."""

    def test_start_marks_worker_alive(
        self, build_outbox, make_sync_writer, collected_writes
    ):
        # Given
        writer = make_sync_writer(collected_writes)
        outbox, _, worker = build_outbox(writer)

        # When
        outbox.start()
        try:
            # Then
            assert worker.is_running is True
            assert worker.is_alive is True
        finally:
            outbox.stop(timeout=1.0)

    def test_start_is_idempotent(
        self, build_outbox, make_sync_writer, collected_writes
    ):
        # Given
        writer = make_sync_writer(collected_writes)
        outbox, _, worker = build_outbox(writer)
        outbox.start()
        first_thread = worker._thread

        try:
            # When — re-entering start does not spawn another thread
            outbox.start()

            # Then
            assert worker._thread is first_thread
        finally:
            outbox.stop(timeout=1.0)

    def test_put_then_flush_drains_through_writer(
        self, build_outbox, make_sync_writer, collected_writes
    ):
        # Given
        writer = make_sync_writer(collected_writes)
        outbox, _, _ = build_outbox(writer, batch_size=2, flush_interval_seconds=0.01)
        outbox.start()
        try:
            # When
            outbox.put({"domain": "payment", "failure_type": "PG_TIMEOUT"})
            outbox.put({"domain": "payment", "failure_type": "PG_TIMEOUT"})
            drained = outbox.flush_and_wait(timeout=2.0)

            # Then
            assert drained >= 2
            assert len(collected_writes) == 2
            assert collected_writes[0]["failure_type"] == "PG_TIMEOUT"
        finally:
            outbox.stop(timeout=1.0)

    def test_stop_is_idempotent(self, build_outbox, make_sync_writer, collected_writes):
        # Given
        writer = make_sync_writer(collected_writes)
        outbox, _, worker = build_outbox(writer)
        outbox.start()

        # When
        outbox.stop(timeout=1.0)
        # Second stop must be a no-op rather than a crash
        remaining = outbox.stop(timeout=1.0)

        # Then
        assert remaining == 0
        assert worker.is_running is False

    # 525 D4: xdist mock_leak — async worker thread races with stats snapshot
    # under -n 6 (entries_written increments only after worker drains the put;
    # project_xdist_isolation pattern).
    @pytest.mark.flaky_quarantine(
        issue="525", first_seen="2026-05-20", category="mock_leak"
    )
    def test_get_stats_returns_full_snapshot(
        self, build_outbox, make_sync_writer, collected_writes
    ):
        # Given
        writer = make_sync_writer(collected_writes)
        outbox, buffer, _ = build_outbox(writer, capacity=50)
        outbox.start()
        try:
            outbox.put({"domain": "x", "failure_type": "y"})

            # When
            stats = outbox.get_stats()
            outbox.flush_and_wait(timeout=2.0)
            stats_after = outbox.get_stats()

            # Then — pre-flush snapshot
            assert isinstance(stats, OutboxStats)
            assert stats.capacity == 50
            assert stats.total_enqueued == 1

            # Then — post-flush snapshot reports the write
            assert stats_after.entries_written >= 1
            assert stats_after.worker_alive is True
            assert stats_after.worker_dead_coercions == 0
        finally:
            outbox.stop(timeout=1.0)

    def test_flush_and_wait_returns_zero_when_empty(
        self, build_outbox, make_sync_writer, collected_writes
    ):
        # Given
        writer = make_sync_writer(collected_writes)
        outbox, _, _ = build_outbox(writer)
        outbox.start()
        try:
            # When — nothing enqueued
            drained = outbox.flush_and_wait(timeout=0.2)

            # Then
            assert drained == 0
        finally:
            outbox.stop(timeout=1.0)

    def test_module_level_flush_and_wait_no_outbox_returns_zero(self):
        # Given — no outbox built
        assert outbox_module._outbox is None

        # When
        drained = flush_and_wait(timeout=0.5)

        # Then
        assert drained == 0


# =============================================================================
# Contract — Outbox.put wraps with enqueue_time tuple
# =============================================================================


class TestOutboxPutContract:
    """Producer wraps payload as ``(enqueue_time, kwargs)`` for D4 delay metric."""

    def test_put_wraps_kwargs_with_enqueue_time(
        self, build_outbox, make_sync_writer, collected_writes
    ):
        # Given
        writer = make_sync_writer(collected_writes)
        outbox, buffer, _ = build_outbox(writer)
        # Worker not started — keep entry in the buffer for inspection.
        kwargs = {"domain": "payment", "failure_type": "PG_TIMEOUT"}

        # When
        before = time.monotonic()
        outbox.put(kwargs)
        after = time.monotonic()

        # Then
        items = buffer.get_all()
        assert len(items) == 1
        enqueue_time, stored_kwargs = items[0]
        assert isinstance(enqueue_time, float)
        assert before <= enqueue_time <= after
        assert stored_kwargs is kwargs

    def test_put_refreshes_current_size_gauge(
        self, build_outbox, make_sync_writer, collected_writes
    ):
        """D4 queue-depth gauge tracks RingBuffer size, refreshed on each put."""
        # Given
        from baldur.services.metrics.definitions import dlq_outbox_current_size

        writer = make_sync_writer(collected_writes)
        outbox, buffer, _ = build_outbox(writer)
        # Worker not started — entries stay buffered so size is deterministic.

        # When
        outbox.put({"domain": "payment", "failure_type": "PG_TIMEOUT"})
        outbox.put({"domain": "payment", "failure_type": "PG_TIMEOUT"})

        # Then — gauge reflects the buffer size observed at the last put
        assert buffer.size == 2
        assert dlq_outbox_current_size._value.get() == 2


# =============================================================================
# Contract — Outbox.from_settings constructs RingBuffer per per-feature settings
# =============================================================================


class TestOutboxFromSettingsContract:
    """``Outbox.from_settings`` reads ``DLQOutboxSettings`` (NOT global RingBufferSettings)."""

    def test_from_settings_builds_drop_oldest_ringbuffer(self):
        # Given
        from baldur.settings.dlq_outbox import DLQOutboxSettings

        captured = {}

        # When — capture the buffer ctor args by patching the source-of-truth
        # module (lazy import target inside from_settings).
        with patch(
            "baldur.settings.dlq_outbox.get_dlq_outbox_settings",
            return_value=DLQOutboxSettings(
                enabled=True,
                capacity=777,
                batch_size=5,
                flush_interval_seconds=0.05,
                drop_rate_threshold=0.07,
                join_timeout_seconds=2.0,
                durable=False,
            ),
        ):
            captured_writer = lambda kwargs: None  # noqa: E731
            outbox = Outbox.from_settings(
                sync_writer=captured_writer,
                emergency_dump=lambda batch, deadline=None: 0,
            )

        try:
            captured["capacity"] = outbox.buffer.capacity
            captured["strategy"] = outbox.buffer._strategy
            # The drop-rate threshold is evaluated by the worker, per drain
            # cycle — the buffer no longer carries an alert callback.
            captured["drop_rate_threshold"] = outbox.worker._drop_rate_threshold
            captured["batch_size"] = outbox.worker._batch_size
            captured["flush_interval"] = outbox.worker._flush_interval

            # Then
            assert captured["capacity"] == 777
            assert captured["strategy"] == BackpressureStrategy.DROP_OLDEST
            assert captured["drop_rate_threshold"] == 0.07
            assert captured["batch_size"] == 5
            assert captured["flush_interval"] == 0.05
        finally:
            # Built outbox not started, but ensure module-state cleanup
            pass


# =============================================================================
# Contract — setup_dlq_outbox idempotency under concurrent re-entry
# =============================================================================


class TestSetupOutboxContract:
    """``setup_dlq_outbox`` is idempotent and races resolve to a single Outbox."""

    def test_setup_first_call_returns_true(self):
        # Given — clean module state (per autouse fixture)
        assert outbox_module._outbox is None

        # When
        with patch(
            "baldur.services.dlq_outbox.outbox._default_sync_writer",
            new=lambda kwargs: None,
        ):
            ok = setup_dlq_outbox()

        # Then
        assert ok is True
        assert outbox_module._outbox is not None

    def test_setup_second_call_returns_false(self):
        # Given
        with patch(
            "baldur.services.dlq_outbox.outbox._default_sync_writer",
            new=lambda kwargs: None,
        ):
            assert setup_dlq_outbox() is True

            # When
            second = setup_dlq_outbox()

        # Then
        assert second is False

    def test_setup_concurrent_invocations_resolve_to_single_outbox(self):
        # Given
        results: list[bool] = []
        outboxes: list[Outbox] = []

        def runner():
            with patch(
                "baldur.services.dlq_outbox.outbox._default_sync_writer",
                new=lambda kwargs: None,
            ):
                results.append(setup_dlq_outbox())
                outboxes.append(outbox_module._outbox)

        threads = [threading.Thread(target=runner) for _ in range(8)]

        # When
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Then — exactly one True, rest False, all observed the same singleton
        assert sum(results) == 1
        assert len({id(ob) for ob in outboxes if ob is not None}) == 1


# =============================================================================
# Behavior — reset_dlq_outbox drains + stops + clears state
# =============================================================================


class TestResetOutboxBehavior:
    """``reset_dlq_outbox`` semantics (D8): drain pending, stop worker, reset flags."""

    def test_reset_returns_zero_when_no_outbox(self):
        # Given
        assert outbox_module._outbox is None

        # When
        remaining = reset_dlq_outbox()

        # Then
        assert remaining == 0

    def test_reset_clears_singleton_after_use(self):
        # Given
        with patch(
            "baldur.services.dlq_outbox.outbox._default_sync_writer",
            new=lambda kwargs: None,
        ):
            setup_dlq_outbox()
            assert outbox_module._outbox is not None

            # When
            reset_dlq_outbox()

        # Then
        assert outbox_module._outbox is None

    def test_reset_resets_worker_dead_state(self):
        # Given — simulate prior dead-worker observation
        outbox_module._worker_dead = True
        outbox_module._worker_dead_coercions = 7

        # When — reset with no outbox still resets the flags (D8 contract)
        reset_dlq_outbox()

        # Then
        assert outbox_module._worker_dead is False
        assert outbox_module._worker_dead_coercions == 0

    def test_reset_drains_pending_entries_before_stop(
        self, build_outbox, make_sync_writer, collected_writes
    ):
        """Drain (D8) — queued entries do not survive into the next test.

        Build an Outbox with a slow sync_writer, enqueue several entries,
        then assign it to the module singleton and call reset. The writer
        must observe the entries before stop() forfeits them.
        """
        # Given
        slow_writer = make_sync_writer(collected_writes)
        outbox, _, _ = build_outbox(
            slow_writer, batch_size=2, flush_interval_seconds=0.01
        )
        outbox.start()
        outbox_module._outbox = outbox  # plug into singleton

        for i in range(5):
            outbox.put({"domain": "payment", "failure_type": f"e{i}"})

        try:
            # When
            reset_dlq_outbox()

            # Then — reset called flush_and_wait(1.0) before stop, so most
            # entries should be drained through the writer. We assert at
            # least one drained (the timing of flush_and_wait can drain all
            # depending on schedule). The contract is "drain, not just clear":
            # flush_and_wait was invoked → writer saw entries.
            assert len(collected_writes) >= 1
        finally:
            outbox_module._outbox = None


# =============================================================================
# Behavior — get_outbox lazy build
# =============================================================================


class TestGetOutboxLazyBuildBehavior:
    """``get_outbox`` builds + starts singleton on first call."""

    def test_lazy_build_creates_and_starts(self):
        # Given
        assert outbox_module._outbox is None

        # When
        with patch(
            "baldur.services.dlq_outbox.outbox._default_sync_writer",
            new=lambda kwargs: None,
        ):
            ob = get_outbox()

        # Then
        assert ob is not None
        assert ob is outbox_module._outbox
        assert ob.worker.is_running is True

    def test_lazy_build_returns_existing_singleton(self):
        # Given
        with patch(
            "baldur.services.dlq_outbox.outbox._default_sync_writer",
            new=lambda kwargs: None,
        ):
            first = get_outbox()
            second = get_outbox()

        # Then
        assert first is second


# =============================================================================
# The process teardown — ``stop_outbox_for_shutdown``
#
# Every exit path calls this unconditionally: the coordinator's handler on a
# signalled exit, and each adapter's exit hook on a recycle exit, which has no
# coordinator window at all. The tests below drive it against an outbox whose
# writer thread was never started, so the rescue is a pure function of the ring
# contents and the injected callbacks — the budget arithmetic is asserted
# against a controlled clock rather than by waiting.
# =============================================================================


class _FakeClock:
    """A monotonic clock the test advances explicitly.

    The budget split is arithmetic over ``time.monotonic()`` readings taken at
    three points inside one call. Waiting for a real clock would either take the
    whole budget or assert nothing; advancing a fake one asserts the split
    exactly.
    """

    def __init__(self, start: float = 10_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _install_outbox(outbox_obj) -> None:
    """Publish a built outbox as the process singleton the teardown reads."""
    outbox_module._outbox = outbox_obj


def _entry(failure_type: str) -> tuple[float, dict]:
    return (time.monotonic(), {"domain": "payment", "failure_type": failure_type})


class TestStopOutboxForShutdownBehavior:
    """Entry-state matrix, the once-guard, and the order of the first step."""

    def test_teardown_coerces_producers_before_it_drains_anything(
        self, build_outbox, make_sync_writer, collected_writes
    ):
        """``_worker_dead`` is set FIRST, ahead of every other step.

        Set last, there is a window between the dump and the flag write in
        which a capture lands in a buffer whose drainer has been joined and
        whose dump has already run — that entry dies with the process, which is
        the exact failure this teardown exists to remove.
        """
        # Given
        outbox, _, worker = build_outbox(make_sync_writer(collected_writes))
        worker._is_running = True
        _install_outbox(outbox)
        observed: list[bool] = []

        def _recording_stop(timeout=5.0, dump_deadline=None):
            observed.append(outbox_module._worker_dead)
            return 0

        # When
        with patch.object(outbox, "stop", side_effect=_recording_stop):
            stop_outbox_for_shutdown()

        # Then — the flag was already set by the time the drain ran
        assert observed == [True]
        assert outbox_module._worker_dead is True

    def test_teardown_worker_dead_flag_is_set_even_with_no_outbox_in_the_process(
        self,
    ):
        """Ahead of the ``is None`` return for the same reason: a process that
        builds the outbox lazily after the teardown began would otherwise get
        an undrained buffer."""
        # Given — nothing was ever built in this process
        assert outbox_module._outbox is None

        # When
        result = stop_outbox_for_shutdown()

        # Then — an all-zero result, not a fabricated drain
        assert outbox_module._worker_dead is True
        assert (
            result.pending_at_entry,
            result.dispatched,
            result.soft_failed,
            result.failed,
            result.emergency_dumped,
            result.residual,
            result.duplicated,
        ) == (0, 0, 0, 0, 0, 0, 0)

    def test_teardown_is_idempotent_and_repeat_callers_get_the_first_result(
        self, build_outbox, make_sync_writer, collected_writes
    ):
        """A second caller receives the FIRST caller's counts, not zeros: that
        result is the terminal report an exit hook logs, and "ran nothing"
        would report an empty drain over a real one."""
        # Given — two entries the dump rescues
        outbox, buffer, worker = build_outbox(
            make_sync_writer(collected_writes),
            on_emergency_dump=lambda batch, deadline=None: len(batch),
        )
        worker._is_running = True
        for i in range(2):
            buffer.put(_entry(f"E{i}"))
        _install_outbox(outbox)

        # When
        first = stop_outbox_for_shutdown()
        second = stop_outbox_for_shutdown()

        # Then
        assert first.emergency_dumped == 2
        assert second is first

    def test_teardown_skips_the_flush_when_the_drainer_is_not_alive(
        self, build_outbox, make_sync_writer, collected_writes
    ):
        """Waiting cannot help a drainer that is not running, and the remainder
        goes to the dump either way."""
        # Given — never started, so ``is_alive`` is False
        outbox, buffer, worker = build_outbox(
            make_sync_writer(collected_writes),
            on_emergency_dump=lambda batch, deadline=None: len(batch),
        )
        worker._is_running = True
        buffer.put(_entry("E0"))
        _install_outbox(outbox)

        # When
        with patch.object(outbox, "flush_and_wait") as m_flush:
            result = stop_outbox_for_shutdown()

        # Then
        m_flush.assert_not_called()
        assert result.emergency_dumped == 1

    def test_teardown_skips_the_flush_when_the_drainer_is_backing_off(
        self, build_outbox, make_sync_writer, collected_writes
    ):
        """A backing-off drainer is one whose writes are already failing, so
        waking it later fails the same writes — skipping is not a concession."""
        # Given
        outbox, buffer, worker = build_outbox(
            make_sync_writer(collected_writes),
            on_emergency_dump=lambda batch, deadline=None: len(batch),
        )
        worker._is_running = True
        worker._consecutive_failures = 99
        buffer.put(_entry("E0"))
        _install_outbox(outbox)

        # When — the drainer reports alive, but is in sustained backoff
        with (
            patch.object(
                type(worker), "is_alive", new_callable=PropertyMock, return_value=True
            ),
            patch.object(outbox, "flush_and_wait") as m_flush,
        ):
            result = stop_outbox_for_shutdown()

        # Then
        m_flush.assert_not_called()
        assert result.emergency_dumped == 1

    def test_teardown_flushes_first_when_the_drainer_can_still_drain(
        self, build_outbox, make_sync_writer, collected_writes
    ):
        """The optimistic phase: entries that can still reach the real DLQ path
        should, rather than being spilled to the local fallback."""
        # Given
        outbox, buffer, worker = build_outbox(make_sync_writer(collected_writes))
        worker._is_running = True
        buffer.put(_entry("E0"))
        _install_outbox(outbox)

        # When
        with (
            patch.object(
                type(worker), "is_alive", new_callable=PropertyMock, return_value=True
            ),
            patch.object(outbox, "flush_and_wait", return_value=1) as m_flush,
            patch.object(outbox, "stop", return_value=0),
        ):
            stop_outbox_for_shutdown()

        # Then
        m_flush.assert_called_once()

    def test_a_raising_flush_does_not_cost_the_teardown_its_dump(
        self, build_outbox, make_sync_writer, collected_writes
    ):
        """The dump is the safety net; an optimistic phase that blew up must
        not take it down with it."""
        # Given
        outbox, buffer, worker = build_outbox(
            make_sync_writer(collected_writes),
            on_emergency_dump=lambda batch, deadline=None: len(batch),
        )
        worker._is_running = True
        buffer.put(_entry("E0"))
        _install_outbox(outbox)

        # When
        with (
            patch.object(
                type(worker), "is_alive", new_callable=PropertyMock, return_value=True
            ),
            patch.object(
                outbox, "flush_and_wait", side_effect=RuntimeError("flush blew up")
            ),
        ):
            result = stop_outbox_for_shutdown()

        # Then
        assert result.emergency_dumped == 1

    def test_a_raising_stop_still_produces_a_terminal_report(
        self, build_outbox, make_sync_writer, collected_writes
    ):
        """The exit hook logs whatever this returns; a raise here would leave
        the hook with nothing to say about the entries."""
        # Given
        outbox, buffer, worker = build_outbox(make_sync_writer(collected_writes))
        worker._is_running = True
        buffer.put(_entry("E0"))
        _install_outbox(outbox)

        # When
        with patch.object(outbox, "stop", side_effect=RuntimeError("stop blew up")):
            result = stop_outbox_for_shutdown()

        # Then — one entry was pending, and nothing claims to have saved it
        assert result.pending_at_entry == 1
        assert result.emergency_dumped == 0
        assert result.dispatched == 0

    def test_reset_clears_the_cached_teardown_result(
        self, build_outbox, make_sync_writer, collected_writes
    ):
        """Kept, the next process-lifetime teardown would return the previous
        one's counts without draining anything."""
        # Given
        outbox, buffer, worker = build_outbox(
            make_sync_writer(collected_writes),
            on_emergency_dump=lambda batch, deadline=None: len(batch),
        )
        worker._is_running = True
        buffer.put(_entry("E0"))
        _install_outbox(outbox)
        assert stop_outbox_for_shutdown().emergency_dumped == 1

        # When
        reset_dlq_outbox()

        # Then
        assert outbox_module._shutdown_result is None
        assert stop_outbox_for_shutdown().emergency_dumped == 0


class TestStopOutboxForShutdownLateProducerBehavior:
    """Two ways a producer can reach the ring after the teardown began.

    The coercion flag closes the window for every producer that reads it after
    step 1. These cover the two paths a producer (or the watchdog on its
    behalf) can still get past it, surfaced by the adversarial pass at
    ``/verify``.
    """

    def test_respawn_event_does_not_clear_the_coercion_once_the_teardown_began(
        self, build_outbox, make_sync_writer, collected_writes
    ):
        """The probe can respawn a drainer that died during the optimistic
        flush — the stopping mark is only set later, inside ``stop()`` — and
        its RESPAWNED event used to flip the flag back. A capture after that
        would be parked in a ring whose drainer is being joined."""
        from types import SimpleNamespace

        # Given — a teardown that has run
        outbox, _, worker = build_outbox(make_sync_writer(collected_writes))
        worker._is_running = True
        _install_outbox(outbox)
        with patch.object(outbox, "stop", return_value=0):
            stop_outbox_for_shutdown()
        assert outbox_module._worker_dead is True

        # When — the watchdog reports a respawn of this worker
        outbox_module._on_daemon_worker_respawned(
            SimpleNamespace(data={"worker_name": "DLQOutboxWorker"})
        )

        # Then — the coercion holds for the rest of the process
        assert outbox_module._worker_dead is True

    def test_respawn_event_still_clears_the_coercion_before_any_teardown(
        self, build_outbox, make_sync_writer, collected_writes
    ):
        """Negative control: the steady-state respawn path is unchanged."""
        from types import SimpleNamespace

        outbox_module._worker_dead = True

        outbox_module._on_daemon_worker_respawned(
            SimpleNamespace(data={"worker_name": "DLQOutboxWorker"})
        )

        assert outbox_module._worker_dead is False

    def test_reset_re_arms_the_respawn_path_for_the_next_lifetime(
        self, build_outbox, make_sync_writer, collected_writes
    ):
        """The teardown mark is process-lifetime state; the test-isolation
        reset must clear it or every later test's respawn path is inert."""
        from types import SimpleNamespace

        stop_outbox_for_shutdown()
        assert outbox_module._teardown_started is True

        reset_dlq_outbox()
        outbox_module._worker_dead = True
        outbox_module._on_daemon_worker_respawned(
            SimpleNamespace(data={"worker_name": "DLQOutboxWorker"})
        )

        assert outbox_module._teardown_started is False
        assert outbox_module._worker_dead is False

    def test_a_put_that_evicts_a_pending_entry_during_the_teardown_is_reported(
        self, build_outbox, make_sync_writer, collected_writes
    ):
        """A producer that passed the coercion check before the flag flipped
        can still land a ``put`` in a full ring. DROP_OLDEST then evicts an
        entry that was pending at entry, the newcomer takes its slot, and the
        seven buckets balance exactly as if nothing happened — so the only
        honest witness is a line naming the drop."""
        from structlog.testing import capture_logs

        # Given — a full ring and a dead drainer
        outbox, buffer, worker = build_outbox(
            make_sync_writer(collected_writes),
            capacity=3,
            on_emergency_dump=lambda batch, deadline=None: len(batch),
        )
        worker._is_running = True
        for i in range(3):
            buffer.put(_entry(f"E{i}"))
        _install_outbox(outbox)
        original_stop = outbox.stop

        def _racing_stop(timeout=5.0, dump_deadline=None):
            # The late producer's put lands before the ring is drained.
            buffer.put(_entry("LATE"))
            return original_stop(timeout=timeout, dump_deadline=dump_deadline)

        # When
        with (
            patch.object(outbox, "stop", side_effect=_racing_stop),
            capture_logs() as cap_logs,
        ):
            result = stop_outbox_for_shutdown()

        # Then — the buckets balance (E0 was substituted, not counted) and the
        # substitution is named
        assert result.pending_at_entry == 3
        assert result.emergency_dumped == 3
        drops = [
            e
            for e in cap_logs
            if e.get("event") == "dlq_outbox.teardown_drops_observed"
        ]
        assert len(drops) == 1
        assert drops[0]["log_level"] == "warning"
        assert drops[0]["dropped"] == 1

    def test_no_drop_line_when_nothing_was_evicted_during_the_teardown(
        self, build_outbox, make_sync_writer, collected_writes
    ):
        """Negative control for the warning above."""
        from structlog.testing import capture_logs

        outbox, buffer, worker = build_outbox(
            make_sync_writer(collected_writes),
            on_emergency_dump=lambda batch, deadline=None: len(batch),
        )
        worker._is_running = True
        buffer.put(_entry("E0"))
        _install_outbox(outbox)

        with capture_logs() as cap_logs:
            stop_outbox_for_shutdown()

        assert not [
            e
            for e in cap_logs
            if e.get("event") == "dlq_outbox.teardown_drops_observed"
        ]


class TestStopOutboxForShutdownConservationBehavior:
    """Every entry the outbox owned at entry ends up in exactly one bucket,
    except where the rescue deliberately double-counts:

        dispatched + soft_failed + failed + emergency_dumped + residual
            == pending_at_entry + duplicated
    """

    def _assert_conserved(self, result) -> None:
        accounted = (
            result.dispatched
            + result.soft_failed
            + result.failed
            + result.emergency_dumped
            + result.residual
        )
        assert accounted == result.pending_at_entry + result.duplicated, (
            f"conservation violated: dispatched={result.dispatched} + "
            f"soft_failed={result.soft_failed} + failed={result.failed} + "
            f"emergency_dumped={result.emergency_dumped} + "
            f"residual={result.residual} != "
            f"pending_at_entry={result.pending_at_entry} + "
            f"duplicated={result.duplicated}"
        )
        assert result.residual >= 0

    def test_conservation_on_a_clean_teardown_is_a_strict_equality(
        self, build_outbox, make_sync_writer, collected_writes
    ):
        """Nothing in flight, so nothing can be counted twice."""
        # Given — three entries on the ring, all rescued by the dump
        outbox, buffer, worker = build_outbox(
            make_sync_writer(collected_writes),
            on_emergency_dump=lambda batch, deadline=None: len(batch),
        )
        worker._is_running = True
        for i in range(3):
            buffer.put(_entry(f"E{i}"))
        _install_outbox(outbox)

        # When
        result = stop_outbox_for_shutdown()

        # Then
        assert result.pending_at_entry == 3
        assert result.emergency_dumped == 3
        assert result.duplicated == 0
        self._assert_conserved(result)

    def test_conservation_reports_a_partial_dump_as_residual_not_as_a_hole(
        self, build_outbox, make_sync_writer, collected_writes
    ):
        """The deadline case: what the dump did not write is named, not
        subtracted away."""
        # Given
        outbox, buffer, worker = build_outbox(
            make_sync_writer(collected_writes),
            on_emergency_dump=lambda batch, deadline=None: 1,
        )
        worker._is_running = True
        for i in range(4):
            buffer.put(_entry(f"E{i}"))
        _install_outbox(outbox)

        # When
        result = stop_outbox_for_shutdown()

        # Then
        assert result.pending_at_entry == 4
        assert result.emergency_dumped == 1
        assert result.residual == 3
        assert result.duplicated == 0
        self._assert_conserved(result)

    def test_conservation_reports_a_dumped_and_written_entry_as_duplicated(
        self, build_outbox
    ):
        """The at-least-once path, end to end.

        A join that times out leaves the drainer alive: it can resolve an entry
        the dump is simultaneously handing to the fallback. Both buckets then
        hold it, which is why the relation is a bound and ``duplicated`` is
        reported instead of surfacing as a negative residual.
        """
        # Given — the writer is blocked inside the only entry of its batch
        inside = threading.Event()
        release = threading.Event()

        def blocking_writer(kwargs):
            inside.set()
            release.wait(timeout=5.0)

        flusher_box: list[threading.Thread] = []

        def dump(batch, deadline=None):
            # The still-alive drainer resolves the same entry while the dump
            # writes it to the fallback tier.
            release.set()
            flusher_box[0].join(timeout=5.0)
            return len(batch)

        outbox, _, worker = build_outbox(blocking_writer, on_emergency_dump=dump)
        worker._is_running = True
        batch = [_entry("IN_FLIGHT")]
        worker._pending_batch = batch
        worker._pending_index = 0
        flusher = threading.Thread(target=worker._flush_batch, args=(batch,))
        flusher_box.append(flusher)
        _install_outbox(outbox)
        flusher.start()
        try:
            assert inside.wait(timeout=5.0)

            # When
            result = stop_outbox_for_shutdown()
        finally:
            release.set()
            flusher.join(timeout=5.0)

        # Then — one pending entry, counted in two buckets, residual not negative
        assert result.pending_at_entry == 1
        assert result.dispatched == 1
        assert result.emergency_dumped == 1
        assert result.duplicated == 1
        assert result.residual == 0
        self._assert_conserved(result)

    def test_conservation_separates_a_soft_store_failure_from_a_write(
        self, build_outbox
    ):
        """A store outage the local fallback absorbed is degraded-but-kept, and
        the terminal report must not fold it into ``dispatched``."""
        # Given — the flush resolves the batch through a failing store
        outbox, buffer, worker = build_outbox(
            lambda kwargs: DLQEntryResult.fallback(
                "db down", "/var/lib/baldur/dlq.jsonl"
            ),
        )
        worker._is_running = True
        batch = [_entry(f"E{i}") for i in range(2)]
        worker._in_flight = 2
        _install_outbox(outbox)

        # When — the drainer's final flush lands during the teardown
        with patch.object(
            outbox, "stop", side_effect=lambda **kw: worker._flush_batch(batch) or 0
        ):
            result = stop_outbox_for_shutdown()

        # Then
        assert result.pending_at_entry == 2
        assert result.soft_failed == 2
        assert result.dispatched == 0
        self._assert_conserved(result)


class TestStopOutboxForShutdownBudgetBehavior:
    """The three-way split of ``join_timeout_seconds`` — flush, join, dump.

    The dump is the safety net, so the phases ahead of it may not spend its
    share: first-come would let a slow flush starve it to zero seconds,
    inverting the priority.
    """

    def _drive(self, outbox, worker, clock, *, timeout=None, alive=False):
        """Run the teardown against a controlled clock, recording the split."""
        seen: dict[str, float] = {}

        def _flush(timeout):
            seen["flush_share"] = timeout
            return 0

        def _stop(timeout=5.0, dump_deadline=None):
            seen["join_share"] = timeout
            seen["dump_deadline"] = dump_deadline
            seen["now_at_stop"] = clock()
            return 0

        stack = [
            patch.object(outbox_module.time, "monotonic", clock),
            patch.object(outbox, "flush_and_wait", side_effect=_flush),
            patch.object(outbox, "stop", side_effect=_stop),
        ]
        if alive:
            stack.append(
                patch.object(
                    type(worker),
                    "is_alive",
                    new_callable=PropertyMock,
                    return_value=True,
                )
            )
        with ExitStack() as es:
            for ctx in stack:
                es.enter_context(ctx)
            stop_outbox_for_shutdown(timeout=timeout)
        return seen

    def test_budget_at_the_shipped_default_leaves_the_dump_its_floor(
        self, build_outbox, make_sync_writer, collected_writes
    ):
        """5.0 s: the flush may spend 3.5, and the two floors (0.5 join,
        1.0 dump) are carved out ahead of it."""
        # Given
        outbox, _, worker = build_outbox(make_sync_writer(collected_writes))
        worker._is_running = True
        _install_outbox(outbox)
        clock = _FakeClock()

        # When
        seen = self._drive(outbox, worker, clock, timeout=5.0, alive=True)

        # Then
        assert seen["flush_share"] == pytest.approx(3.5)
        assert seen["dump_deadline"] - seen["now_at_stop"] >= (
            seen["join_share"] + outbox_module._MIN_DUMP_SECONDS
        )

    def test_budget_at_the_settings_range_floor_still_runs_a_bounded_teardown(
        self, build_outbox, make_sync_writer, collected_writes
    ):
        """0.1 s is below the two floors combined, so the floors dominate and
        the whole teardown is ~1.5 s rather than 0.1 s. Documented, not
        accidental: a budget that starves the dump would defeat it."""
        # Given
        outbox, _, worker = build_outbox(make_sync_writer(collected_writes))
        worker._is_running = True
        _install_outbox(outbox)
        clock = _FakeClock()
        started = clock()

        # When
        seen = self._drive(outbox, worker, clock, timeout=0.1, alive=True)

        # Then — the flush gets nothing, the join its floor, the dump its floor
        assert "flush_share" not in seen
        assert seen["join_share"] == pytest.approx(outbox_module._MIN_STOP_JOIN_SECONDS)
        assert seen["dump_deadline"] - started == pytest.approx(
            outbox_module._MIN_STOP_JOIN_SECONDS + outbox_module._MIN_DUMP_SECONDS
        )

    def test_budget_at_the_settings_range_top_is_honoured_in_full(
        self, build_outbox, make_sync_writer, collected_writes
    ):
        """60.0 s is the field's ceiling; the split must scale to it rather
        than saturate at some internal cap."""
        # Given
        outbox, _, worker = build_outbox(make_sync_writer(collected_writes))
        worker._is_running = True
        _install_outbox(outbox)
        clock = _FakeClock()
        started = clock()

        # When
        seen = self._drive(outbox, worker, clock, timeout=60.0, alive=True)

        # Then
        assert seen["flush_share"] == pytest.approx(58.5)
        assert seen["dump_deadline"] - started == pytest.approx(60.0)

    def test_budget_dump_floor_survives_a_flush_that_consumed_everything(
        self, build_outbox, make_sync_writer, collected_writes
    ):
        """The floor that stops a slow flush from starving the safety net: the
        deadline is pushed past the join rather than left in the past."""
        # Given — the flush burns the entire budget
        outbox, _, worker = build_outbox(make_sync_writer(collected_writes))
        worker._is_running = True
        _install_outbox(outbox)
        clock = _FakeClock()
        started = clock()

        seen: dict[str, float] = {}

        def _flush(timeout):
            # Overshoots the whole budget, which a slow local fallback does.
            clock.advance(5.5)
            return 0

        def _stop(timeout=5.0, dump_deadline=None):
            seen["join_share"] = timeout
            seen["dump_deadline"] = dump_deadline
            seen["now_at_stop"] = clock()
            return 0

        # When
        with (
            patch.object(outbox_module.time, "monotonic", clock),
            patch.object(
                type(worker), "is_alive", new_callable=PropertyMock, return_value=True
            ),
            patch.object(outbox, "flush_and_wait", side_effect=_flush),
            patch.object(outbox, "stop", side_effect=_stop),
        ):
            stop_outbox_for_shutdown(timeout=5.0)

        # Then — the budget is already spent, yet the dump still gets its floor
        # past the end of the join
        assert seen["now_at_stop"] > started + 5.0
        assert seen["join_share"] == pytest.approx(outbox_module._MIN_STOP_JOIN_SECONDS)
        assert seen["dump_deadline"] - seen["now_at_stop"] == pytest.approx(
            outbox_module._MIN_STOP_JOIN_SECONDS + outbox_module._MIN_DUMP_SECONDS
        )

    def test_budget_defaults_to_the_configured_join_timeout(
        self, build_outbox, make_sync_writer, collected_writes
    ):
        """``timeout=None`` is what every production caller passes, so the
        settings field is the knob an operator actually turns."""
        # Given
        from baldur.settings.dlq_outbox import DLQOutboxSettings

        outbox, _, worker = build_outbox(make_sync_writer(collected_writes))
        worker._is_running = True
        _install_outbox(outbox)
        clock = _FakeClock()
        started = clock()
        configured = DLQOutboxSettings(join_timeout_seconds=12.0)

        # When
        with patch(
            "baldur.settings.dlq_outbox.get_dlq_outbox_settings",
            return_value=configured,
        ):
            seen = self._drive(outbox, worker, clock, timeout=None, alive=True)

        # Then
        assert seen["flush_share"] == pytest.approx(10.5)
        assert seen["dump_deadline"] - started == pytest.approx(12.0)

    def test_budget_falls_back_to_the_shipped_default_when_settings_cannot_be_read(
        self, build_outbox, make_sync_writer, collected_writes
    ):
        """A degenerate config still gets a bounded teardown rather than an
        unbounded one."""
        # Given
        outbox, _, worker = build_outbox(make_sync_writer(collected_writes))
        worker._is_running = True
        _install_outbox(outbox)
        clock = _FakeClock()
        started = clock()

        # When
        with patch(
            "baldur.settings.dlq_outbox.get_dlq_outbox_settings",
            side_effect=RuntimeError("settings blew up"),
        ):
            seen = self._drive(outbox, worker, clock, timeout=None, alive=True)

        # Then
        assert seen["dump_deadline"] - started == pytest.approx(
            outbox_module._FALLBACK_TEARDOWN_BUDGET_SECONDS
        )


class TestStopOutboxForShutdownGateBehavior:
    """The once-guard is its own lock, deliberately not ``_outbox_lock``."""

    def test_gate_not_outbox_lock_singleton_access_is_not_blocked_by_a_teardown(
        self, build_outbox, make_sync_writer, collected_writes
    ):
        """Holding the singleton-construction lock across a blocking teardown
        would put a first-time ``get_outbox()`` build, a ``setup_dlq_outbox()``
        re-entry and ``reset_dlq_outbox()`` behind the whole drain.

        ``get_outbox()`` short-circuits before the lock once a singleton
        exists, so ``setup_dlq_outbox()`` is what actually probes
        ``_outbox_lock`` here; both are asserted.
        """
        # Given — a teardown parked inside its drain
        outbox, _, worker = build_outbox(make_sync_writer(collected_writes))
        worker._is_running = True
        _install_outbox(outbox)

        in_drain = threading.Event()
        release = threading.Event()
        probed: list[str] = []

        def _blocking_stop(timeout=5.0, dump_deadline=None):
            in_drain.set()
            release.wait(timeout=5.0)
            return 0

        def _teardown():
            with patch.object(outbox, "stop", side_effect=_blocking_stop):
                stop_outbox_for_shutdown()

        tearer = threading.Thread(target=_teardown)
        tearer.start()
        try:
            assert in_drain.wait(timeout=5.0)

            # When — another thread touches the singleton surface
            def _probe():
                get_outbox()
                setup_dlq_outbox()
                probed.append("done")

            prober = threading.Thread(target=_probe)
            prober.start()
            prober.join(timeout=2.0)

            # Then — it did not have to wait for the drain
            assert probed == ["done"], (
                "the singleton lock was held across the teardown's drain"
            )
        finally:
            release.set()
            tearer.join(timeout=5.0)

    def test_a_second_teardown_blocks_until_the_first_publishes_its_result(
        self, build_outbox, make_sync_writer, collected_writes
    ):
        """The gate serializes rather than short-circuits: a caller arriving
        mid-drain must report the real counts, not "someone else is doing it"."""
        # Given
        outbox, buffer, worker = build_outbox(
            make_sync_writer(collected_writes),
            on_emergency_dump=lambda batch, deadline=None: len(batch),
        )
        worker._is_running = True
        buffer.put(_entry("E0"))
        _install_outbox(outbox)

        in_drain = threading.Event()
        release = threading.Event()
        second: list = []

        original_stop = outbox.stop

        def _blocking_stop(timeout=5.0, dump_deadline=None):
            in_drain.set()
            release.wait(timeout=5.0)
            return original_stop(timeout=timeout, dump_deadline=dump_deadline)

        def _first():
            with patch.object(outbox, "stop", side_effect=_blocking_stop):
                stop_outbox_for_shutdown()

        first_thread = threading.Thread(target=_first)
        first_thread.start()
        try:
            assert in_drain.wait(timeout=5.0)
            second_thread = threading.Thread(
                target=lambda: second.append(stop_outbox_for_shutdown())
            )
            second_thread.start()
            second_thread.join(timeout=0.3)

            # Then — the second caller is still waiting on the gate
            assert second_thread.is_alive()
            assert second == []
        finally:
            release.set()
            first_thread.join(timeout=5.0)

        second_thread.join(timeout=5.0)
        assert second[0].emergency_dumped == 1


class TestOutboxShutdownReserveContract:
    """``get_shutdown_reserve_seconds()`` — what an exit path holds back.

    A step that waits on other subsystems has to reserve the budget of the step
    behind it, or the teardown is the first thing an external watcher cuts.
    """

    def test_reserve_is_the_configured_budget_plus_the_dump_floor(self):
        # Given
        from baldur.settings.dlq_outbox import DLQOutboxSettings

        # When
        with patch(
            "baldur.settings.dlq_outbox.get_dlq_outbox_settings",
            return_value=DLQOutboxSettings(join_timeout_seconds=10.0),
        ):
            reserve = get_shutdown_reserve_seconds()

        # Then — 10.0 budget + 1.0 dump floor
        assert reserve == pytest.approx(11.0)

    def test_reserve_at_the_shipped_default_is_six_seconds(self):
        """The number the runbooks size ``--timeout`` against."""
        from baldur.settings.dlq_outbox import DLQOutboxSettings

        with patch(
            "baldur.settings.dlq_outbox.get_dlq_outbox_settings",
            return_value=DLQOutboxSettings(),
        ):
            assert get_shutdown_reserve_seconds() == pytest.approx(6.0)

    def test_reserve_falls_back_to_the_shipped_default_on_a_settings_failure(self):
        """A degenerate config must not make the reserve zero — that would put
        the teardown back at the front of the guillotine."""
        with patch(
            "baldur.settings.dlq_outbox.get_dlq_outbox_settings",
            side_effect=RuntimeError("settings blew up"),
        ):
            assert get_shutdown_reserve_seconds() == pytest.approx(6.0)


class TestOutboxShutdownResultContract:
    """The terminal report's shape — seven named buckets, no derived field."""

    def test_result_declares_exactly_the_seven_terminal_buckets(self):
        from dataclasses import fields

        assert [f.name for f in fields(OutboxShutdownResult)] == [
            "pending_at_entry",
            "dispatched",
            "soft_failed",
            "failed",
            "emergency_dumped",
            "residual",
            "duplicated",
        ]

    def test_result_is_frozen_so_a_cached_report_cannot_be_rewritten(self):
        """The first caller's result is handed to every later caller; a mutable
        one would let an exit hook edit what another hook already logged."""
        result = OutboxShutdownResult(1, 1, 0, 0, 0, 0, 0)

        with pytest.raises(Exception):
            result.dispatched = 99
