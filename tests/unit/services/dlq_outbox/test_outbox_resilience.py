"""DLQOutboxWorker resilience tests (impl doc 486 D11).

Covers Test Assessment rows:
- ``TestOutboxResilienceWorkerLoopBehavior`` — D11.1: per-iteration error containment
  (thread survives unhandled ``sync_writer`` exception)
- ``TestOutboxResilienceBackoffBehavior`` /
  ``TestOutboxResilienceBackoffContract`` — D11.2: ``ExponentialBackoff``
  engages after ``_FAILURE_BACKOFF_THRESHOLD`` consecutive failures; Contract
  class pins design-doc constants.
- ``TestOutboxResilienceShutdownBehavior`` — D11.3: graceful-shutdown emergency
  dump via ``on_emergency_dump`` callback (production wires
  ``DLQService._write_to_local_fallback``)
- ``TestOutboxWorkerDeathAlertBehavior`` — impl 489 D8: dlq_outbox subscribes
  to the cross-shape ``DAEMON_WORKER_DIED`` / ``DAEMON_WORKER_RESPAWNED``
  events; producer-side ``_worker_dead`` flag toggles in response. The
  CRITICAL log + EventBus emit are now owned by ``DaemonWorkerProbe`` (not
  ``check_worker_liveness`` — removed in 489).
"""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, patch

from baldur.services.dlq_outbox import outbox as outbox_module
from baldur.services.dlq_outbox.outbox import (
    Outbox,
    is_worker_dead,
    record_worker_dead_coercion,
)
from baldur.services.dlq_outbox.worker import (
    _FAILURE_BACKOFF_THRESHOLD,
    DLQOutboxWorker,
)

# =============================================================================
# D11.1 — per-iteration error containment
# =============================================================================


class TestOutboxResilienceWorkerLoopBehavior:
    """Worker thread MUST never die on a transient ``sync_writer`` exception."""

    def test_worker_loop_survives_unhandled_sync_writer_exception(
        self, build_outbox, make_sync_writer, collected_writes
    ):
        """First entry raises; subsequent entry processes successfully."""
        # Given
        writer = make_sync_writer(collected_writes, raise_n=1)
        outbox, _, worker = build_outbox(
            writer, batch_size=1, flush_interval_seconds=0.01
        )
        outbox.start()
        try:
            # When
            outbox.put({"domain": "payment", "failure_type": "first"})
            outbox.put({"domain": "payment", "failure_type": "second"})

            # Then — wait for second entry to be processed
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and len(collected_writes) < 1:
                time.sleep(0.01)

            assert worker.is_alive is True
            assert worker.entries_failed >= 1
            assert worker.entries_written >= 1
            # Only the second entry made it to the sink (first raised)
            assert len(collected_writes) == 1
            assert collected_writes[0]["failure_type"] == "second"
        finally:
            outbox.stop(timeout=1.0)

    def test_writer_loop_survives_buffer_get_batch_exception(
        self, build_outbox, make_sync_writer, collected_writes
    ):
        """If ``buffer.get_batch`` raises, the loop logs + continues."""
        # Given
        writer = make_sync_writer(collected_writes)
        outbox, buffer, worker = build_outbox(
            writer, batch_size=1, flush_interval_seconds=0.01
        )

        original_get_batch = buffer.get_batch
        call_count = {"n": 0}

        def flaky_get_batch(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("transient buffer error")
            return original_get_batch(*args, **kwargs)

        buffer.get_batch = flaky_get_batch
        outbox.start()
        try:
            # When
            outbox.put({"domain": "x", "failure_type": "y"})

            # Then — wait for entry to land despite the first raise
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and len(collected_writes) < 1:
                time.sleep(0.01)

            assert worker.is_alive is True
            assert len(collected_writes) == 1
        finally:
            outbox.stop(timeout=1.0)


# =============================================================================
# D11.2 — ExponentialBackoff after consecutive failures
# =============================================================================


class TestOutboxResilienceBackoffBehavior:
    """Worker engages backoff sleep after ``_FAILURE_BACKOFF_THRESHOLD`` consecutive failures."""

    def test_consecutive_failures_increment_on_each_failed_batch(
        self, build_outbox, make_sync_writer, collected_writes
    ):
        # Given
        writer = make_sync_writer(collected_writes, always_raise=True)
        outbox, _, worker = build_outbox(
            writer, batch_size=1, flush_interval_seconds=0.01
        )
        outbox.start()
        try:
            # When — enqueue several entries that all fail
            for i in range(4):
                outbox.put({"domain": "payment", "failure_type": f"e{i}"})

            # Wait until worker has processed all 4 entries
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and worker.entries_failed < 4:
                time.sleep(0.01)

            # Then
            assert worker.entries_failed == 4
            assert worker.consecutive_failures >= _FAILURE_BACKOFF_THRESHOLD
        finally:
            outbox.stop(timeout=1.0)

    def test_consecutive_failures_reset_on_success(
        self, build_outbox, make_sync_writer, collected_writes
    ):
        """After N failures, a successful flush resets the counter."""
        # Given — first 2 invocations raise, 3rd succeeds
        writer = make_sync_writer(collected_writes, raise_n=2)
        outbox, _, worker = build_outbox(
            writer, batch_size=1, flush_interval_seconds=0.01
        )
        outbox.start()
        try:
            # When
            outbox.put({"domain": "payment", "failure_type": "fail1"})
            outbox.put({"domain": "payment", "failure_type": "fail2"})
            outbox.put({"domain": "payment", "failure_type": "succeed"})

            # Wait for the success entry
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and len(collected_writes) < 1:
                time.sleep(0.01)

            # Then
            assert worker.consecutive_failures == 0
            assert worker.entries_failed == 2
            assert worker.entries_written == 1
        finally:
            outbox.stop(timeout=1.0)


class TestOutboxResilienceBackoffContract:
    """Design-doc constants from impl doc 486 D11.2."""

    def test_backoff_constants_match_design_doc(self):
        """Contract: 486 D11.2 specifies threshold=3, base=0.1s, max=10s."""
        from baldur.services.dlq_outbox.worker import (
            _BACKOFF_BASE_DELAY,
            _BACKOFF_MAX_DELAY,
            _FAILURE_BACKOFF_THRESHOLD,
        )

        assert _FAILURE_BACKOFF_THRESHOLD == 3
        assert _BACKOFF_BASE_DELAY == 0.1
        assert _BACKOFF_MAX_DELAY == 10.0


# =============================================================================
# D11.3 — graceful-shutdown emergency dump
# =============================================================================


class TestOutboxResilienceShutdownBehavior:
    """``stop(timeout)`` must drain remaining entries via ``on_emergency_dump``."""

    def test_stop_invokes_emergency_dump_with_remaining_entries(self):
        # Given — sync_writer that hangs forever so worker can't drain in time
        from baldur.audit.ring_buffer import RingBuffer
        from baldur.settings.backpressure import BackpressureStrategy

        forever = threading.Event()
        stop_signal = threading.Event()

        def hanging_writer(kwargs):
            stop_signal.wait(timeout=10.0)
            forever.wait(timeout=10.0)

        dumped: list[list[dict]] = []
        buffer: RingBuffer = RingBuffer(
            capacity=100, strategy=BackpressureStrategy.DROP_OLDEST
        )
        worker = DLQOutboxWorker(
            buffer=buffer,
            sync_writer=hanging_writer,
            batch_size=1,
            flush_interval_seconds=0.01,
            on_emergency_dump=dumped.append,
        )
        outbox = Outbox(buffer=buffer, worker=worker)
        outbox.start()
        try:
            # When — fill buffer; worker can pick up at most 1 (held by hanging_writer)
            for i in range(5):
                outbox.put({"domain": "payment", "failure_type": f"e{i}"})

            # Give worker time to grab the first entry into hanging_writer
            time.sleep(0.05)

            # Stop with very short timeout — remaining entries should be dumped
            stop_signal.set()  # not strictly needed; let writer hang
            remaining = outbox.stop(timeout=0.05)

            # Then — emergency_dump received the remaining queued kwargs
            assert remaining >= 1
            assert len(dumped) >= 1
            dumped_kwargs = dumped[0]
            assert all(isinstance(k, dict) for k in dumped_kwargs)
            assert all("failure_type" in k for k in dumped_kwargs)
        finally:
            forever.set()  # release hanging writer for clean test exit

    def test_stop_with_no_pending_entries_returns_zero(
        self, build_outbox, make_sync_writer, collected_writes
    ):
        # Given
        writer = make_sync_writer(collected_writes)
        outbox, _, _ = build_outbox(writer)
        outbox.start()

        # When
        remaining = outbox.stop(timeout=1.0)

        # Then
        assert remaining == 0

    def test_stop_swallows_emergency_dump_failure(self):
        """If ``on_emergency_dump`` raises, ``stop`` must still return cleanly."""
        # Given
        from baldur.audit.ring_buffer import RingBuffer
        from baldur.settings.backpressure import BackpressureStrategy

        forever = threading.Event()

        def hanging_writer(kwargs):
            forever.wait(timeout=10.0)

        def failing_dump(batch):
            raise RuntimeError("dump failed")

        buffer: RingBuffer = RingBuffer(
            capacity=100, strategy=BackpressureStrategy.DROP_OLDEST
        )
        worker = DLQOutboxWorker(
            buffer=buffer,
            sync_writer=hanging_writer,
            batch_size=1,
            flush_interval_seconds=0.01,
            on_emergency_dump=failing_dump,
        )
        outbox = Outbox(buffer=buffer, worker=worker)
        outbox.start()
        try:
            outbox.put({"domain": "payment", "failure_type": "x"})
            outbox.put({"domain": "payment", "failure_type": "y"})
            time.sleep(0.05)

            # When / Then — stop must not raise even though dump raises
            outbox.stop(timeout=0.05)
        finally:
            forever.set()

    def test_deferred_partial_batch_survives_stop_and_is_flushed(
        self, build_outbox, make_sync_writer, collected_writes
    ):
        """2c (impl doc 559): a partial batch RETAINED by ``_drain_once`` (D1) is
        not lost on shutdown — the final drain (D4) flushes it. Exercises the
        D1-retained-state -> D4-final-drain interaction; maps to scenario row
        1.8 (graceful-shutdown in-flight zero-loss).
        """
        # Given — batch_size>1 + a large flush_interval, so the single entry is
        # never due during the test and the worker keeps it retained in the
        # buffer (the new D1 deferred state) rather than discarding it.
        writer = make_sync_writer(collected_writes)
        outbox, buffer, worker = build_outbox(
            writer, batch_size=5, flush_interval_seconds=10.0
        )
        outbox.start()
        try:
            outbox.put({"domain": "payment", "failure_type": "deferred"})

            # Let the worker run a deferring iteration. With a 10s interval it
            # cannot flush, so the entry is retained (zero loss, not discarded).
            time.sleep(0.05)
            assert buffer.size == 1
            assert worker.entries_written == 0

            # When — stop preempts the idle wait; the final drain flushes the
            # retained entry.
            remaining = outbox.stop(timeout=1.0)

            # Then — the deferred entry landed, nothing was lost.
            assert worker.entries_written == 1
            assert buffer.size == 0
            assert remaining == 0
            assert collected_writes == [
                {"domain": "payment", "failure_type": "deferred"}
            ]
            # Conservation across the D1-retained -> D4 final-drain interaction.
            # Clean stop drained everything, so emergency_dumped stays 0.
            stats = outbox.get_stats()
            assert stats.entries_emergency_dumped == 0
            assert stats.total_enqueued == (
                stats.entries_written
                + stats.entries_failed
                + stats.total_dropped
                + stats.size
                + stats.in_flight
                + stats.entries_emergency_dumped
            )
        finally:
            outbox.stop(timeout=1.0)

    def test_emergency_dumped_entries_are_accounted_in_conservation(self):
        """impl doc 559 (C): entries removed via the shutdown emergency-dump path
        are counted in ``OutboxStats.entries_emergency_dumped`` so the
        conservation invariant stays closed across a stop()-timeout dump — a
        monitor asserting it never sees a phantom shortfall after graceful
        shutdown. Without the terminal bucket the dumped entries would vanish
        from ``size`` into no account (total_enqueued > sum of buckets).
        """
        from baldur.audit.ring_buffer import RingBuffer
        from baldur.settings.backpressure import BackpressureStrategy

        # Given — a writer that hangs on the first popped entry so the worker
        # cannot drain the rest within the stop timeout, forcing the remainder
        # down the emergency-dump path.
        forever = threading.Event()

        def hanging_writer(kwargs):
            forever.wait(timeout=10.0)

        dumped: list[list[dict]] = []
        buffer: RingBuffer = RingBuffer(
            capacity=100, strategy=BackpressureStrategy.DROP_OLDEST
        )
        worker = DLQOutboxWorker(
            buffer=buffer,
            sync_writer=hanging_writer,
            batch_size=1,
            flush_interval_seconds=0.01,
            on_emergency_dump=dumped.append,
        )
        outbox = Outbox(buffer=buffer, worker=worker)
        outbox.start()
        try:
            for i in range(5):
                outbox.put({"domain": "payment", "failure_type": f"e{i}"})
            # Let the worker grab the first entry into the hanging writer.
            time.sleep(0.05)

            # When — stop with a short timeout: 1 entry is stuck in-flight in the
            # hanging writer, the other 4 are still buffered and get dumped.
            remaining = outbox.stop(timeout=0.05)

            # Then — the dumped count is surfaced and conservation holds with the
            # in-flight (stuck write) + emergency_dumped (buffered remainder)
            # terms both contributing.
            assert remaining >= 1
            assert len(dumped) >= 1
            stats = outbox.get_stats()
            assert stats.entries_emergency_dumped == remaining
            assert stats.total_enqueued == (
                stats.entries_written
                + stats.entries_failed
                + stats.total_dropped
                + stats.size
                + stats.in_flight
                + stats.entries_emergency_dumped
            )
        finally:
            forever.set()  # release the hanging writer for clean test exit


# =============================================================================
# D11.4 — worker thread death alert + producer fail-open coercion
# =============================================================================


class TestOutboxWorkerDeathAlertBehavior:
    """impl 489 D8: ``_worker_dead`` flag toggles via EventBus subscription.

    The cross-shape ``DaemonWorkerProbe`` owns CRITICAL-log + emit duties
    (tested in ``tests/unit/meta/``); these tests cover the dlq_outbox-side
    contract: handlers wired in ``setup_dlq_outbox()`` flip the producer
    fail-open flag for the matching ``worker_name``.
    """

    def _make_event(self, worker_name: str) -> MagicMock:
        event = MagicMock()
        event.data = {"worker_name": worker_name}
        return event

    def test_worker_dead_flag_starts_false(self):
        """Module-level ``_worker_dead`` defaults to False."""
        assert is_worker_dead() is False

    def test_died_event_for_dlq_outbox_worker_sets_flag_true(self):
        """``DAEMON_WORKER_DIED`` for ``DLQOutboxWorker`` flips ``_worker_dead``."""
        outbox_module._worker_dead = False
        try:
            outbox_module._on_daemon_worker_died(self._make_event("DLQOutboxWorker"))
            assert is_worker_dead() is True
        finally:
            outbox_module._worker_dead = False

    def test_died_event_for_other_worker_is_ignored(self):
        """Death events for other workers must not toggle dlq_outbox's flag."""
        outbox_module._worker_dead = False
        try:
            outbox_module._on_daemon_worker_died(self._make_event("AuditWatchdog"))
            assert is_worker_dead() is False
        finally:
            outbox_module._worker_dead = False

    def test_respawned_event_for_dlq_outbox_worker_sets_flag_false(self):
        """``DAEMON_WORKER_RESPAWNED`` for ``DLQOutboxWorker`` clears the flag."""
        outbox_module._worker_dead = True
        try:
            outbox_module._on_daemon_worker_respawned(
                self._make_event("DLQOutboxWorker")
            )
            assert is_worker_dead() is False
        finally:
            outbox_module._worker_dead = False

    def test_respawned_event_for_other_worker_is_ignored(self):
        """Respawn events for other workers must not clear dlq_outbox's flag."""
        outbox_module._worker_dead = True
        try:
            outbox_module._on_daemon_worker_respawned(self._make_event("AuditWatchdog"))
            assert is_worker_dead() is True
        finally:
            outbox_module._worker_dead = False

    def test_setup_wires_both_subscribers_on_event_bus(self):
        """``setup_dlq_outbox()`` subscribes both lifecycle handlers."""
        from baldur.services.event_bus.bus.event_types import EventType

        mock_bus = MagicMock()
        with (
            patch(
                "baldur.services.event_bus.bus.convenience.get_event_bus",
                return_value=mock_bus,
            ),
        ):
            outbox_module._wire_worker_lifecycle_subscribers()

        subscribed_event_types = {
            call.args[0] for call in mock_bus.subscribe.call_args_list
        }
        assert EventType.DAEMON_WORKER_DIED in subscribed_event_types
        assert EventType.DAEMON_WORKER_RESPAWNED in subscribed_event_types

    def test_record_worker_dead_coercion_increments_counter(self):
        """``record_worker_dead_coercion`` bumps both module and Prometheus counters."""
        # Given
        assert outbox_module._worker_dead_coercions == 0
        mock_counter = MagicMock()

        # When
        with patch(
            "baldur.services.metrics.definitions.dlq_outbox_worker_dead_coercions_total",
            mock_counter,
        ):
            record_worker_dead_coercion()
            record_worker_dead_coercion()

        # Then
        assert outbox_module._worker_dead_coercions == 2
        assert mock_counter.inc.call_count == 2

    def test_record_worker_dead_coercion_swallows_metric_error(self):
        """Metric error must not break the producer dispatch path."""
        # Given
        with patch(
            "baldur.services.metrics.definitions.dlq_outbox_worker_dead_coercions_total",
            new=MagicMock(inc=MagicMock(side_effect=RuntimeError("metric down"))),
        ):
            # When / Then — does not raise
            record_worker_dead_coercion()
        assert outbox_module._worker_dead_coercions >= 1
