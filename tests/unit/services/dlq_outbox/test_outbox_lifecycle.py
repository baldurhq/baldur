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
from unittest.mock import patch

import pytest

from baldur.services.dlq_outbox import outbox as outbox_module
from baldur.services.dlq_outbox.outbox import (
    Outbox,
    OutboxStats,
    flush_and_wait,
    get_outbox,
    reset_dlq_outbox,
    setup_dlq_outbox,
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
                emergency_dump=lambda batch: None,
            )

        try:
            captured["capacity"] = outbox.buffer.capacity
            captured["strategy"] = outbox.buffer._strategy
            captured["drop_rate_threshold"] = outbox.buffer._drop_rate_threshold
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
