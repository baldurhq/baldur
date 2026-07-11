"""DLQOutboxWorker lifecycle unit tests (impl 489 D9).

Test targets:
    - DLQOutboxWorker._spawn_thread — rebinds handle.thread on respawn
    - DLQOutboxWorker.stop — orders is_stopping → join → unregister → is_alive log
    - DLQOutboxWorker._writer_loop_with_crash_capture — populates last_crash_reason
      for BaseException; re-raises (KeyboardInterrupt, SystemExit) without recording

These complete the Test Assessment rows that the e2e suite only touches
indirectly:
    - TestDLQOutboxWorkerSpawnHelperBehavior
    - TestDLQOutboxWorkerStopOrderBehavior
    - TestDLQOutboxWorkerCrashCaptureBehavior
"""

from __future__ import annotations

import threading
import time
from unittest.mock import patch

import pytest

from baldur.metrics.recorders.daemon_worker import (
    get_registered_daemon_workers,
)


@pytest.fixture(autouse=True)
def _clean_handle_registry():
    """Snapshot+clear the handle registry around each test."""
    from baldur.metrics.recorders import daemon_worker as mod

    with mod._registry_lock:
        snapshot = dict(mod._handle_registry)
        mod._handle_registry.clear()
    yield
    with mod._registry_lock:
        mod._handle_registry.clear()
        mod._handle_registry.update(snapshot)


# =============================================================================
# Behavior — _spawn_thread rebinds handle.thread on respawn
# =============================================================================


class TestDLQOutboxWorkerSpawnHelperBehavior:
    """impl 489 D9: ``_spawn_thread`` constructs a fresh thread + rebinds handle."""

    def test_spawn_thread_creates_running_thread_named_dlq_outbox_worker(
        self, build_outbox, make_sync_writer, collected_writes
    ):
        """``_spawn_thread`` starts a daemon thread named ``DLQOutboxWorker``."""
        # Given
        writer = make_sync_writer(collected_writes)
        _, _, worker = build_outbox(writer, flush_interval_seconds=0.01)

        # When
        worker.start()
        try:
            # Then
            assert worker._thread is not None
            assert worker._thread.is_alive()
            assert worker._thread.daemon is True
            assert worker._thread.name == "DLQOutboxWorker"
        finally:
            worker.stop(timeout=1.0)

    def test_spawn_thread_called_again_rebinds_handle_thread(
        self, build_outbox, make_sync_writer, collected_writes
    ):
        """Respawn (re-call ``_spawn_thread``) updates ``handle.thread``."""
        # Given
        writer = make_sync_writer(collected_writes)
        _, _, worker = build_outbox(writer, flush_interval_seconds=0.01)
        worker.start()
        try:
            original_thread = worker._thread
            handle = worker.handle
            assert handle is not None
            assert handle.thread is original_thread

            # When — simulate the respawn coordinator calling the helper
            # (which is exactly what ``handle.restart_callback`` points at).
            worker._spawn_thread()

            # Then — the worker's thread reference is the new thread, AND
            # the handle's thread reference rebinds to it (without this,
            # the next probe tick would still see the old thread).
            assert worker._thread is not original_thread
            assert handle.thread is worker._thread
            assert worker._thread.is_alive()
        finally:
            worker.stop(timeout=1.0)
            # Drain the orphan thread from the first start() — it is a daemon
            # thread but we wait briefly so the test does not leak it visibly.
            original_thread.join(timeout=1.0)

    def test_handle_restart_callback_points_at_spawn_thread(
        self, build_outbox, make_sync_writer, collected_writes
    ):
        """``handle.restart_callback`` is exactly ``worker._spawn_thread``.

        Per D4 / R9 — the callback MUST NOT point at ``start()`` (which has
        a ``_is_running`` early-return that would silently no-op on
        respawn).
        """
        # Given
        writer = make_sync_writer(collected_writes)
        _, _, worker = build_outbox(writer, flush_interval_seconds=0.01)

        # When
        worker.start()
        try:
            # Then
            assert worker.handle is not None
            assert worker.handle.restart_callback == worker._spawn_thread
            # And it is NOT pointing at the public start().
            assert worker.handle.restart_callback != worker.start
        finally:
            worker.stop(timeout=1.0)


# =============================================================================
# Behavior — stop() ordering: is_stopping → join → unregister → is_alive log
# =============================================================================


class TestDLQOutboxWorkerStopOrderBehavior:
    """impl 489 D9: ``stop()`` orders side effects to avoid spurious UNHEALTHY."""

    def test_stop_sets_is_stopping_before_join(
        self, build_outbox, make_sync_writer, collected_writes
    ):
        """``handle.is_stopping`` is True at the moment ``thread.join`` is invoked.

        D9 mandates is_stopping → join → unregister so a probe tick caught
        between the running flag flip and the unregister observes STOPPING
        rather than firing UNHEALTHY/respawn.
        """
        # Given
        writer = make_sync_writer(collected_writes)
        _, _, worker = build_outbox(writer, flush_interval_seconds=0.01)
        worker.start()
        handle = worker.handle
        assert handle is not None
        assert handle.is_stopping is False

        observed_is_stopping_at_join: list[bool] = []
        original_join = worker._thread.join

        def recording_join(*args, **kwargs):
            observed_is_stopping_at_join.append(handle.is_stopping)
            return original_join(*args, **kwargs)

        # When
        with patch.object(worker._thread, "join", side_effect=recording_join):
            worker.stop(timeout=1.0)

        # Then — at the moment join() ran, is_stopping had already flipped True.
        assert observed_is_stopping_at_join == [True]

    def test_stop_unregisters_after_join_completes(
        self, build_outbox, make_sync_writer, collected_writes
    ):
        """The handle is unregistered AFTER ``thread.join`` returns."""
        # Given
        writer = make_sync_writer(collected_writes)
        _, _, worker = build_outbox(writer, flush_interval_seconds=0.01)
        worker.start()
        assert "DLQOutboxWorker" in get_registered_daemon_workers()

        original_join = worker._thread.join
        registry_state_during_join: list[bool] = []

        def recording_join(*args, **kwargs):
            registry_state_during_join.append(
                "DLQOutboxWorker" in get_registered_daemon_workers()
            )
            return original_join(*args, **kwargs)

        # When
        with patch.object(worker._thread, "join", side_effect=recording_join):
            worker.stop(timeout=1.0)

        # Then — handle was still registered while join() ran, gone after.
        assert registry_state_during_join == [True]
        assert "DLQOutboxWorker" not in get_registered_daemon_workers()

    def test_stop_logs_critical_when_thread_outlives_join_timeout(
        self, build_outbox, make_sync_writer, collected_writes
    ):
        """If the thread is still alive after ``join``, log CRITICAL ``stop_join_timeout``."""
        # Given
        writer = make_sync_writer(collected_writes)
        _, _, worker = build_outbox(writer, flush_interval_seconds=0.01)
        worker.start()

        # When — replace the worker's join with a no-op so the thread
        # appears "still alive" after the call returns. Replace is_alive
        # to also report True after the no-op join.
        with (
            patch.object(worker._thread, "join", return_value=None),
            patch.object(worker._thread, "is_alive", return_value=True),
            patch("baldur.services.dlq_outbox.worker.logger") as mock_logger,
        ):
            worker.stop(timeout=0.1)

        # Then — CRITICAL log fired with the stop_join_timeout event name
        # and worker_name + join_timeout_seconds in the payload.
        critical_calls = [
            c
            for c in mock_logger.critical.call_args_list
            if c.args and c.args[0] == "daemon_worker.stop_join_timeout"
        ]
        assert len(critical_calls) == 1
        kwargs = critical_calls[0].kwargs
        assert kwargs["worker_name"] == "DLQOutboxWorker"
        assert kwargs["join_timeout_seconds"] == 0.1

        # Force the actual thread to terminate so the test does not leak it.
        worker._stop_event.set()
        worker._thread.join(timeout=2.0)


# =============================================================================
# Behavior — _writer_loop_with_crash_capture
# =============================================================================


class TestDLQOutboxWorkerCrashCaptureBehavior:
    """impl 489 D4: crash-capture wrapper records BaseException only.

    ``(KeyboardInterrupt, SystemExit)`` re-raise WITHOUT calling
    ``record_crash`` — those signals are normal shutdown paths and must
    not produce misleading ``crash_reason`` payloads in ``DAEMON_WORKER_DIED``.
    """

    def test_value_error_in_writer_loop_populates_last_crash_reason(
        self, build_outbox, make_sync_writer, collected_writes
    ):
        """Uncaught ``ValueError`` in the loop target → ``handle.last_crash_reason`` set."""
        # Given
        writer = make_sync_writer(collected_writes)
        _, _, worker = build_outbox(writer, flush_interval_seconds=0.01)
        worker.start()
        # Quiesce the spawned loop before patching _writer_loop to raise: the
        # crash-capture wrapper is exercised directly below, so a live loop
        # thread would only race to hit the patch (an unhandled thread
        # exception) or linger past the test. stop() leaves the handle intact.
        worker.stop(timeout=1.0)
        handle = worker.handle
        assert handle is not None
        assert handle.last_crash_reason is None

        # When — patch ``_writer_loop`` to raise ValueError, then invoke the
        # crash-capture wrapper directly. The wrapper re-raises after
        # recording, so we expect ValueError to surface here.
        with (
            patch.object(worker, "_writer_loop", side_effect=ValueError("boom")),
            pytest.raises(ValueError, match="boom"),
        ):
            worker._writer_loop_with_crash_capture()

        # Then
        assert handle.last_crash_reason == "ValueError: boom"

    def test_runtime_error_populates_last_crash_reason(
        self, build_outbox, make_sync_writer, collected_writes
    ):
        """Any subclass of ``Exception`` is captured."""
        # Given
        writer = make_sync_writer(collected_writes)
        _, _, worker = build_outbox(writer, flush_interval_seconds=0.01)
        worker.start()
        # Quiesce the spawned loop before patching _writer_loop to raise (see
        # the ValueError case above for the rationale). stop() keeps the handle.
        worker.stop(timeout=1.0)
        handle = worker.handle
        assert handle is not None

        # When
        with (
            patch.object(worker, "_writer_loop", side_effect=RuntimeError("flushed")),
            pytest.raises(RuntimeError),
        ):
            worker._writer_loop_with_crash_capture()

        # Then
        assert handle.last_crash_reason == "RuntimeError: flushed"

    @pytest.mark.parametrize("exc_cls", [KeyboardInterrupt, SystemExit])
    def test_keyboard_interrupt_and_system_exit_reraise_without_recording(
        self, exc_cls, build_outbox, make_sync_writer, collected_writes
    ):
        """``KeyboardInterrupt`` / ``SystemExit`` re-raise; ``last_crash_reason`` stays None."""
        # Given
        writer = make_sync_writer(collected_writes)
        _, _, worker = build_outbox(writer, flush_interval_seconds=0.01)
        worker.start()
        # Quiesce the spawned loop before patching _writer_loop to raise (see
        # the ValueError case above for the rationale). stop() keeps the handle.
        worker.stop(timeout=1.0)
        handle = worker.handle
        assert handle is not None
        assert handle.last_crash_reason is None

        # When
        with (
            patch.object(worker, "_writer_loop", side_effect=exc_cls()),
            pytest.raises(exc_cls),
        ):
            worker._writer_loop_with_crash_capture()

        # Then — signal-driven shutdown is NOT a crash; payload stays clean.
        assert handle.last_crash_reason is None

    def test_baseexception_subclass_other_than_signals_is_captured(
        self, build_outbox, make_sync_writer, collected_writes
    ):
        """A custom ``BaseException`` subclass (not signal) is captured."""

        class WeirdAbort(BaseException):
            pass

        writer = make_sync_writer(collected_writes)
        _, _, worker = build_outbox(writer, flush_interval_seconds=0.01)
        worker.start()
        # Quiesce the spawned loop before patching _writer_loop to raise (see
        # the ValueError case above for the rationale). stop() keeps the handle.
        worker.stop(timeout=1.0)
        handle = worker.handle
        assert handle is not None

        with (
            patch.object(worker, "_writer_loop", side_effect=WeirdAbort("odd")),
            pytest.raises(WeirdAbort),
        ):
            worker._writer_loop_with_crash_capture()

        assert handle.last_crash_reason == "WeirdAbort: odd"


# =============================================================================
# Behavior — entry conservation (impl doc 559: D1 size-check+conditional-pop, D6
# in_flight accounting). The size-check->decide->flush core lives in the
# synchronous ``_drain_once`` seam, so these tests drive it directly — no daemon
# thread, no sleep, no timing flake (D3). The conservation invariant they assert
# is
#     total_enqueued == entries_written + entries_failed + total_dropped
#                       + size + in_flight + entries_emergency_dumped
# which D1/D6 make continuously true — across normal operation AND shutdown
# (zero silent worker-loop loss).
# =============================================================================


def _assert_conserved(outbox) -> None:
    """Assert the 559 conservation invariant holds for ``outbox``'s stats."""
    s = outbox.get_stats()
    assert s.total_enqueued == (
        s.entries_written
        + s.entries_failed
        + s.total_dropped
        + s.size
        + s.in_flight
        + s.entries_emergency_dumped
    ), (
        f"conservation violated: total_enqueued={s.total_enqueued} != "
        f"written={s.entries_written} + failed={s.entries_failed} + "
        f"dropped={s.total_dropped} + size={s.size} + in_flight={s.in_flight} "
        f"+ emergency_dumped={s.entries_emergency_dumped}"
    )


class TestDLQOutboxWorkerEntryConservationBehavior:
    """impl doc 559 D1/D3/D6: ``_drain_once`` never silently discards a popped
    batch, and ``in_flight`` closes the pop->increment accounting window."""

    @pytest.mark.parametrize(
        (
            "n_entries",
            "batch_size",
            "last_flush_offset",
            "expected_flushed",
            "expected_size",
            "expected_written",
        ),
        [
            # empty buffer never flushes, even with the interval long elapsed
            (0, 5, 100.0, False, 0, 0),
            # partial batch, interval NOT elapsed -> deferred (retained)
            (1, 5, 0.0, False, 1, 0),
            # partial batch, interval elapsed -> flush by time
            (1, 5, 100.0, True, 0, 1),
            # full by size -> flush regardless of the (un-elapsed) interval
            (5, 5, 0.0, True, 0, 5),
        ],
        ids=["empty", "partial-not-due", "partial-due-by-time", "full-by-size"],
    )
    def test_drain_once_should_flush_decision_matrix(
        self,
        build_outbox,
        make_sync_writer,
        collected_writes,
        n_entries,
        batch_size,
        last_flush_offset,
        expected_flushed,
        expected_size,
        expected_written,
    ):
        """``_drain_once`` flushes iff the buffer is non-empty AND (full-by-size
        OR ``flush_interval`` elapsed); otherwise the partial batch is retained.
        """
        # Given — a long flush_interval so "elapsed" is driven solely by an
        # aged ``last_flush`` (no time mocking, per Testability Notes).
        writer = make_sync_writer(collected_writes)
        outbox, buffer, worker = build_outbox(
            writer, batch_size=batch_size, flush_interval_seconds=10.0
        )
        for i in range(n_entries):
            outbox.put({"domain": "payment", "failure_type": f"e{i}"})

        # When
        last_flush = time.monotonic() - last_flush_offset
        new_last_flush, flushed = worker._drain_once(last_flush)

        # Then
        assert flushed is expected_flushed
        assert buffer.size == expected_size
        assert worker.entries_written == expected_written
        assert worker.in_flight == 0
        # last_flush advances only on an actual flush; otherwise unchanged.
        if expected_flushed:
            assert new_last_flush >= last_flush
        else:
            assert new_last_flush == last_flush
        _assert_conserved(outbox)

    def test_drain_once_defers_partial_batch_then_flushes_it_after_interval(
        self, build_outbox, make_sync_writer, collected_writes
    ):
        """Regression for 559: a not-yet-due partial batch is RETAINED in the
        buffer (pre-fix: popped then silently discarded), and the SAME entry is
        flushed once the interval elapses — written, never lost.
        """
        # Given — batch_size>1 + a large interval reproduces the drop condition.
        writer = make_sync_writer(collected_writes)
        outbox, buffer, worker = build_outbox(
            writer, batch_size=5, flush_interval_seconds=10.0
        )
        outbox.put({"domain": "payment", "failure_type": "deferred"})

        # When (1) — interval not elapsed -> defer
        last_flush = time.monotonic()
        last_flush, flushed = worker._drain_once(last_flush)

        # Then (1) — entry retained, nothing written, zero loss. The pre-fix
        # code left buffer.size==0 AND entries_written==0 here (lost).
        assert flushed is False
        assert buffer.size == 1
        assert worker.entries_written == 0
        assert worker.in_flight == 0
        assert collected_writes == []
        _assert_conserved(outbox)

        # When (2) — interval elapsed -> flush the same deferred entry
        aged_last_flush = time.monotonic() - 100.0
        _, flushed_again = worker._drain_once(aged_last_flush)

        # Then (2) — the deferred entry is now written, not dropped
        assert flushed_again is True
        assert buffer.size == 0
        assert worker.entries_written == 1
        assert worker.in_flight == 0
        assert collected_writes == [{"domain": "payment", "failure_type": "deferred"}]
        _assert_conserved(outbox)

    def test_drain_once_failed_writes_decrement_in_flight_once_each(
        self, build_outbox, make_sync_writer, collected_writes
    ):
        """D6: ``_flush_batch`` decrements ``in_flight`` once per entry via the
        per-entry ``finally`` even when every write fails, so a fully-failed
        batch settles to ``in_flight == 0`` and the invariant still closes.
        """
        # Given — writer raises on every entry
        writer = make_sync_writer(collected_writes, always_raise=True)
        outbox, buffer, worker = build_outbox(
            writer, batch_size=5, flush_interval_seconds=10.0
        )
        outbox.put({"domain": "payment", "failure_type": "f1"})
        outbox.put({"domain": "payment", "failure_type": "f2"})

        # When — aged last_flush forces the flush; both writes raise
        _, flushed = worker._drain_once(time.monotonic() - 100.0)

        # Then — both counted as failed, in_flight fully drained, nothing lost
        assert flushed is True
        assert worker.entries_failed == 2
        assert worker.entries_written == 0
        assert worker.in_flight == 0
        assert buffer.size == 0
        _assert_conserved(outbox)  # 2 == 0 + 2 + 0 + 0 + 0

    def test_flush_and_wait_blocks_until_in_flight_drains_no_undercount(
        self, build_outbox
    ):
        """D6: while an entry is mid-write the buffer is already empty
        (``size==0``) but ``in_flight==1``; ``flush_and_wait`` must block on the
        ``in_flight`` term and only then report a settled (non-undercounted)
        drained delta. The conservation invariant holds at every sample.
        """
        # Given — a writer that signals entry then blocks until released, so the
        # pop->increment window is held open deterministically.
        entered = threading.Event()
        release = threading.Event()

        def blocking_writer(kwargs):
            entered.set()
            release.wait(timeout=5.0)

        outbox, buffer, worker = build_outbox(
            blocking_writer, batch_size=1, flush_interval_seconds=0.01
        )
        outbox.start()
        try:
            # When — enqueue one entry and wait until the worker is mid-write
            outbox.put({"domain": "payment", "failure_type": "blocked"})
            assert entered.wait(timeout=2.0), "worker never entered the write"

            # Then — buffer drained but the entry is still in flight (not yet
            # written), and the invariant holds across that window.
            assert buffer.size == 0
            assert worker.in_flight == 1
            assert worker.entries_written == 0
            _assert_conserved(outbox)  # 1 == 0 + 0 + 0 + 0 + 1

            # And — flush_and_wait must NOT return while in_flight > 0.
            result: dict[str, int] = {}

            def do_flush():
                result["drained"] = outbox.flush_and_wait(timeout=3.0)

            flush_thread = threading.Thread(target=do_flush)
            flush_thread.start()
            flush_thread.join(timeout=0.2)
            assert flush_thread.is_alive(), (
                "flush_and_wait returned while the entry was still in flight"
            )
            assert "drained" not in result

            # When — release the write; the entry lands.
            release.set()
            flush_thread.join(timeout=2.0)

            # Then — flush_and_wait reports the entry as drained (no undercount),
            # in_flight is back to 0, and the invariant closes.
            assert result["drained"] == 1
            assert worker.in_flight == 0
            assert worker.entries_written == 1
            assert buffer.size == 0
            _assert_conserved(outbox)  # 1 == 1 + 0 + 0 + 0 + 0
        finally:
            release.set()
            outbox.stop(timeout=1.0)

    def test_drain_once_flushes_get_batch_actual_result_not_a_stale_view(
        self, build_outbox, make_sync_writer, collected_writes
    ):
        """2b: the ``should_flush`` decision reads ``size`` non-destructively, but
        the flush uses ``get_batch``'s ACTUAL result. If a DROP_OLDEST eviction
        shifts the front between the size read and the pop, the worker flushes
        exactly what ``get_batch`` returns (counted once), never a stale view —
        validating D1's "displaced front is an observable drop, never silent
        loss" safety claim.
        """
        # Given — a real entry so ``size`` (>0) drives should_flush.
        writer = make_sync_writer(collected_writes)
        outbox, buffer, worker = build_outbox(
            writer, batch_size=5, flush_interval_seconds=10.0
        )
        outbox.put({"domain": "payment", "failure_type": "A"})
        popped = [(time.monotonic(), {"domain": "payment", "failure_type": "B"})]

        # When — get_batch returns [B], diverging from the buffer's real front
        # (simulating a front displaced by a DROP_OLDEST eviction between the
        # size read and the pop). size==1 < batch_size, so the flush is driven
        # by the elapsed interval (aged last_flush).
        with patch.object(buffer, "get_batch", return_value=popped):
            _, flushed = worker._drain_once(time.monotonic() - 100.0)

        # Then — flushed get_batch's [B]; counted exactly once, no double-count.
        assert flushed is True
        assert collected_writes == [{"domain": "payment", "failure_type": "B"}]
        assert worker.entries_written == 1
        assert worker.in_flight == 0

    def test_in_flight_property_and_outbox_stats_field_default_zero(
        self, build_outbox, make_sync_writer, collected_writes
    ):
        """Contract (D6): ``in_flight`` and ``entries_emergency_dumped`` on both
        the worker and ``OutboxStats`` are 0 on a fresh worker, and the stats
        fields are sourced from the worker counters.
        """
        # Given
        writer = make_sync_writer(collected_writes)
        outbox, _, worker = build_outbox(writer)

        # Then — fresh worker is idle
        assert worker.in_flight == 0
        assert worker.entries_emergency_dumped == 0
        assert outbox.get_stats().in_flight == 0
        assert outbox.get_stats().entries_emergency_dumped == 0

        # And — the stats fields mirror the worker counters (wiring check)
        worker._in_flight = 3
        worker._entries_emergency_dumped = 2
        assert outbox.get_stats().in_flight == 3
        assert outbox.get_stats().entries_emergency_dumped == 2
