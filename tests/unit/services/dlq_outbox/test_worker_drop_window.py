"""Worker-side drop accounting for the outbox ring buffer.

Drops used to be reported by the buffer's own threshold callback, and could
not be relied on for three independent reasons: the alert latched once per
process, the rate it tested divided lifetime totals (so a late burst read as
near-zero no matter how the latch was re-armed), and the counter counted
alerts rather than entries.

The drainer owns it instead. Each cycle it reads the buffer's public counters
and reports the window since its last read — which is what makes the alert
re-armable by construction, the count exact by summation, and the whole thing
run on a thread nobody is waiting on.

The producer-thread half matters as much as the numbers: the buffer's own
callback runs inside ``put``'s lock on whichever request thread overflowed the
ring, and the alert does a WARNING log plus an awaited event emit that blocks
up to the per-handler timeout.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

from baldur.audit.ring_buffer import RingBuffer, RingBufferStats
from baldur.services.dlq_outbox.worker import DLQOutboxWorker, DropWindow
from baldur.settings.backpressure import BackpressureStrategy

# =============================================================================
# Helpers
# =============================================================================


class _CountedBuffer(RingBuffer):
    """A real ring whose reported counters the test advances by hand.

    ``get_stats()`` is the only surface the accounting touches — a public,
    lock-scoped counter read — so overriding it is enough to drive any drop
    history without filling a ring 10 million times.
    """

    def __init__(self, capacity: int = 100) -> None:
        super().__init__(capacity=capacity, strategy=BackpressureStrategy.DROP_OLDEST)
        self.reported_enqueued = 0
        self.reported_dropped = 0
        self.reported_size = 0

    def advance(self, *, enqueued: int = 0, dropped: int = 0) -> None:
        self.reported_enqueued += enqueued
        self.reported_dropped += dropped

    def get_stats(self) -> RingBufferStats:
        return RingBufferStats(
            capacity=self._capacity,
            size=self.reported_size,
            total_enqueued=self.reported_enqueued,
            total_dropped=self.reported_dropped,
            drop_rate=(
                self.reported_dropped / self.reported_enqueued
                if self.reported_enqueued
                else 0.0
            ),
        )


def _drops_observed_callback(dropped: int) -> None:
    """Signature spec for the drop-count callback mock."""


def _drop_alert_callback(window: DropWindow) -> None:
    """Signature spec for the windowed-alert callback mock."""


def _observer(**kwargs) -> MagicMock:
    return MagicMock(spec=_drops_observed_callback, **kwargs)


def _alerter(**kwargs) -> MagicMock:
    return MagicMock(spec=_drop_alert_callback, **kwargs)


def _worker(
    buffer: RingBuffer,
    *,
    on_drops_observed=None,
    on_drop_alert=None,
    drop_rate_threshold: float = 0.01,
) -> DLQOutboxWorker:
    """A worker wired for accounting only — its thread is never started."""
    return DLQOutboxWorker(
        buffer=buffer,
        sync_writer=lambda kwargs: None,
        on_drops_observed=on_drops_observed,
        on_drop_alert=on_drop_alert,
        drop_rate_threshold=drop_rate_threshold,
    )


# =============================================================================
# Behavior — per-cycle delta accounting
# =============================================================================


class TestOutboxDropWindowBehavior:
    """``_observe_drop_window`` — delta arithmetic, guards, and containment."""

    def test_first_cycle_reports_every_drop_so_far(self):
        buffer = _CountedBuffer()
        buffer.advance(enqueued=200, dropped=50)
        observed = _observer()
        worker = _worker(buffer, on_drops_observed=observed)

        worker._observe_drop_window()

        observed.assert_called_once_with(50)

    def test_second_cycle_reports_only_the_new_drops(self):
        """Exact by summation: the counter must read as entries lost, so a
        cycle may never re-report what the previous one already published."""
        buffer = _CountedBuffer()
        buffer.advance(enqueued=200, dropped=50)
        observed = _observer()
        worker = _worker(buffer, on_drops_observed=observed)
        worker._observe_drop_window()
        observed.reset_mock()

        buffer.advance(enqueued=100, dropped=30)
        worker._observe_drop_window()

        observed.assert_called_once_with(30)

    def test_summed_windows_equal_the_lifetime_drop_count(self):
        buffer = _CountedBuffer()
        reported: list[int] = []
        worker = _worker(buffer, on_drops_observed=reported.append)

        for _ in range(3):
            buffer.advance(enqueued=100, dropped=7)
            worker._observe_drop_window()

        assert sum(reported) == buffer.reported_dropped == 21

    def test_a_window_with_no_drops_reports_nothing(self):
        buffer = _CountedBuffer()
        buffer.advance(enqueued=500)
        observed = _observer()
        worker = _worker(buffer, on_drops_observed=observed)

        worker._observe_drop_window()

        observed.assert_not_called()

    def test_late_drop_episode_still_breaches_the_threshold(self):
        """The defect this repair exists for: 5,000 drops after 10M clean
        enqueues is 0.0005 of the process lifetime and would stay silent — but
        it is half of ITS window, and that is what an operator needs to see."""
        buffer = _CountedBuffer()
        alert = _alerter()
        worker = _worker(buffer, on_drop_alert=alert)
        buffer.advance(enqueued=10_000_000)
        worker._observe_drop_window()
        alert.assert_not_called()

        buffer.advance(enqueued=10_000, dropped=5_000)
        worker._observe_drop_window()

        assert alert.call_count == 1
        window: DropWindow = alert.call_args.args[0]
        assert window.dropped == 5_000
        assert window.enqueued == 10_000
        assert window.drop_rate == 0.5
        # The lifetime rate the replaced evaluation used stays far below
        # the same threshold — which is why it never fired.
        assert buffer.get_stats().drop_rate < 0.01

    def test_the_alert_re_arms_every_window(self):
        """A latched alert made every episode after the first invisible."""
        buffer = _CountedBuffer()
        alert = _alerter()
        worker = _worker(buffer, on_drop_alert=alert)

        for _ in range(3):
            buffer.advance(enqueued=100, dropped=50)
            worker._observe_drop_window()

        assert alert.call_count == 3

    def test_a_window_below_the_threshold_does_not_alert(self):
        buffer = _CountedBuffer()
        alert = _alerter()
        worker = _worker(buffer, on_drop_alert=alert, drop_rate_threshold=0.1)
        buffer.advance(enqueued=1000, dropped=50)

        worker._observe_drop_window()

        alert.assert_not_called()

    def test_threshold_boundary_is_exclusive(self):
        """At exactly the threshold nothing fires; one drop more does."""
        buffer = _CountedBuffer()
        alert = _alerter()
        worker = _worker(buffer, on_drop_alert=alert, drop_rate_threshold=0.01)

        buffer.advance(enqueued=1000, dropped=10)
        worker._observe_drop_window()
        assert alert.call_count == 0

        buffer.advance(enqueued=1000, dropped=11)
        worker._observe_drop_window()
        assert alert.call_count == 1

    def test_a_zero_enqueue_window_is_not_evaluated(self):
        """A drop happens only inside ``put``, which counts the enqueue
        first — so an idle window has no drops and no 0/0 to divide."""
        buffer = _CountedBuffer()
        alert = _alerter()
        worker = _worker(buffer, on_drop_alert=alert)

        worker._observe_drop_window()
        worker._observe_drop_window()

        alert.assert_not_called()

    def test_alert_payload_carries_both_window_and_lifetime_numbers(self):
        buffer = _CountedBuffer()
        buffer.reported_size = 42
        alert = _alerter()
        worker = _worker(buffer, on_drop_alert=alert)
        buffer.advance(enqueued=200, dropped=50)

        worker._observe_drop_window()

        window: DropWindow = alert.call_args.args[0]
        assert window.dropped == 50
        assert window.enqueued == 200
        assert window.drop_rate == 0.25
        assert window.total_dropped == 50
        assert window.capacity == 100
        assert window.size == 42

    def test_an_accounting_failure_does_not_disrupt_the_cycle(self):
        buffer = _CountedBuffer()
        buffer.advance(enqueued=200, dropped=50)
        alert = _alerter()
        worker = _worker(
            buffer,
            on_drops_observed=_observer(side_effect=RuntimeError("metrics down")),
            on_drop_alert=alert,
        )

        worker._observe_drop_window()

        # The cycle continued: the alert half still ran.
        alert.assert_called_once()

    def test_an_alert_failure_does_not_disrupt_the_cycle(self):
        buffer = _CountedBuffer()
        buffer.advance(enqueued=200, dropped=50)
        worker = _worker(
            buffer,
            on_drop_alert=_alerter(side_effect=RuntimeError("bus dead")),
        )

        # Then — does not raise, and the watermark still advanced.
        worker._observe_drop_window()

        assert worker._last_seen_dropped == 50

    def test_missing_callbacks_are_tolerated(self):
        """Both callbacks are optional — a worker built without them must
        still complete its cycle and keep its watermarks current."""
        buffer = _CountedBuffer()
        buffer.advance(enqueued=200, dropped=50)
        worker = _worker(buffer)

        worker._observe_drop_window()

        assert worker._last_seen_dropped == 50

    def test_watermarks_restart_after_a_fork_repair(self):
        """The child's buffer counters restart with its contents, so kept
        watermarks would make the first window negative and hide this
        process's drops until it caught up."""
        buffer = _CountedBuffer()
        buffer.advance(enqueued=10_000, dropped=900)
        observed = _observer()
        worker = _worker(buffer, on_drops_observed=observed)
        worker._observe_drop_window()
        observed.reset_mock()

        # The child inherits the worker object; its buffer starts empty.
        worker._buffer = _CountedBuffer()
        worker.repair_after_fork()
        try:
            worker._buffer.advance(enqueued=100, dropped=5)
            worker._observe_drop_window()
        finally:
            worker.stop(timeout=2.0)

        observed.assert_called_once_with(5)

    def test_the_drain_loop_accounts_every_cycle(self):
        """Wiring: the accounting runs from the loop, not only when called
        by hand — otherwise nothing would ever publish a window."""
        buffer = _CountedBuffer()
        observed: list[int] = []
        worker = _worker(buffer, on_drops_observed=observed.append)
        buffer.advance(enqueued=100, dropped=20)

        worker.start()
        try:
            deadline = time.monotonic() + 2.0
            while not observed and time.monotonic() < deadline:
                time.sleep(0.01)
        finally:
            worker.stop(timeout=2.0)

        assert observed
        assert observed[0] == 20


# =============================================================================
# Behavior — the window the loop never gets to
# =============================================================================


class TestOutboxDropWindowAtShutdownBehavior:
    """The last window has no next cycle to report it.

    "Visibility can lag by a cycle, never vanish" only holds while another
    cycle is coming. Everything the ring drops while the loop sits in its
    final ``wait`` is reported by nobody unless the exit path closes the
    window — and a process shutting down under load is exactly when a full
    ring is dropping.
    """

    def test_drops_during_the_final_wait_still_reach_the_counter(self):
        observed: list[int] = []
        buffer: RingBuffer = RingBuffer(
            capacity=5, strategy=BackpressureStrategy.DROP_OLDEST
        )
        worker = DLQOutboxWorker(
            buffer=buffer,
            sync_writer=lambda kwargs: None,
            batch_size=1,
            # Long enough that the overflow below lands inside one wait.
            flush_interval_seconds=0.5,
            on_drops_observed=observed.append,
        )

        worker.start()
        try:
            deadline = time.monotonic() + 2.0
            while not worker.is_alive and time.monotonic() < deadline:
                time.sleep(0.01)
            # Overflow the ring while the loop is parked in stop_event.wait().
            for i in range(20):
                buffer.put((0.0, {"i": i}))
            dropped = buffer.get_stats().total_dropped
        finally:
            worker.stop(timeout=2.0)

        assert dropped > 0
        assert sum(observed) == dropped

    def test_the_shutdown_observation_does_not_alert(self):
        """The counter update is what makes the loss visible afterwards; the
        alert would emit onto a bus being torn down, on the thread ``stop()``
        is waiting to join."""
        buffer = _CountedBuffer()
        alerter = _alerter()
        worker = _worker(buffer, on_drop_alert=alerter)
        buffer.advance(enqueued=100, dropped=90)

        worker._observe_drop_window(alert=False)

        alerter.assert_not_called()

    def test_the_shutdown_observation_still_counts(self):
        buffer = _CountedBuffer()
        observed = _observer()
        worker = _worker(buffer, on_drops_observed=observed)
        buffer.advance(enqueued=100, dropped=90)

        worker._observe_drop_window(alert=False)

        observed.assert_called_once_with(90)


# =============================================================================
# Behavior — the producer thread pays nothing
# =============================================================================


class TestOutboxProducerThreadBehavior:
    """The alert must never run under the ring lock on a request thread."""

    def test_the_shipped_buffer_carries_no_producer_side_drop_callback(self):
        """Wiring assertion: the outbox builds its ring without the threshold
        callback, so ``put`` cannot reach the alert at all."""
        from baldur.services.dlq_outbox.outbox import Outbox

        outbox = Outbox.from_settings(sync_writer=lambda kwargs: None)

        assert outbox._buffer._on_drop_threshold is None

    def test_a_ring_overflow_runs_nothing_but_the_eviction(self):
        """Control on the buffer itself: with no callback wired, overflowing
        it from a producer thread costs only the eviction."""
        buffer: RingBuffer = RingBuffer(
            capacity=2, strategy=BackpressureStrategy.DROP_OLDEST
        )

        for i in range(500):
            buffer.put((0.0, {"i": i}))

        assert buffer.get_stats().total_dropped == 498
