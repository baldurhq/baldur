"""Outbox drop policy + observability tests (impl doc 486 D4 + G11).

Covers Test Assessment rows:
- ``TestOutboxDropPolicyBehavior`` — DROP_OLDEST eviction + drop-rate threshold callback
  (log + Prometheus counter + EventBus event)
- ``TestOutboxObservabilityBehavior`` — ``dlq_outbox_processing_delay_seconds``
  observed when worker pops entries (D4 leading indicator)
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from baldur.audit.ring_buffer import RingBuffer, RingBufferStats
from baldur.services.dlq_outbox.outbox import (
    _on_drop_threshold,
    _on_processing_delay,
)
from baldur.settings.backpressure import BackpressureStrategy

# =============================================================================
# Behavior — DROP_OLDEST eviction + drop-threshold callback
# =============================================================================


class TestOutboxDropPolicyBehavior:
    """RingBuffer drop semantics + ``_on_drop_threshold`` callback wiring."""

    def test_capacity_minus_one_does_not_trigger_drop(self):
        """Boundary: filling to ``capacity - 1`` keeps drop count at 0."""
        # Given
        buffer: RingBuffer = RingBuffer(
            capacity=10,
            strategy=BackpressureStrategy.DROP_OLDEST,
        )

        # When
        for i in range(9):
            buffer.put((0.0, {"i": i}))

        # Then
        stats = buffer.get_stats()
        assert stats.size == 9
        assert stats.total_dropped == 0

    def test_capacity_plus_one_triggers_single_drop(self):
        """Boundary: filling to ``capacity + 1`` evicts exactly one entry."""
        # Given
        buffer: RingBuffer = RingBuffer(
            capacity=10,
            strategy=BackpressureStrategy.DROP_OLDEST,
        )

        # When
        for i in range(11):
            buffer.put((0.0, {"i": i}))

        # Then
        stats = buffer.get_stats()
        assert stats.size == 10
        assert stats.total_dropped == 1

    def test_drop_oldest_evicts_first_inserted(self):
        """DROP_OLDEST: when full, the *oldest* entry is evicted."""
        # Given
        buffer: RingBuffer = RingBuffer(
            capacity=3, strategy=BackpressureStrategy.DROP_OLDEST
        )
        for i in range(3):
            buffer.put((0.0, {"i": i}))

        # When — overflow by one
        buffer.put((0.0, {"i": 99}))

        # Then — oldest (i=0) gone, newest (i=99) present
        items = buffer.get_all()
        assert len(items) == 3
        ids = [k["i"] for _, k in items]
        assert 0 not in ids
        assert 99 in ids

    def test_threshold_callback_fires_only_after_min_samples(self):
        """``MIN_SAMPLES_FOR_ALERT=100`` gate prevents premature alerts."""
        # Given
        callback = MagicMock()
        buffer: RingBuffer = RingBuffer(
            capacity=2,
            strategy=BackpressureStrategy.DROP_OLDEST,
            on_drop_threshold=callback,
            drop_rate_threshold=0.01,
        )

        # When — only 50 enqueues, even with many drops, fewer than MIN_SAMPLES
        for i in range(50):
            buffer.put((0.0, {"i": i}))

        # Then
        callback.assert_not_called()

    def test_threshold_callback_fires_once_past_threshold(self):
        """Callback fires exactly once (alert_sent latch)."""
        # Given
        callback = MagicMock()
        buffer: RingBuffer = RingBuffer(
            capacity=10,
            strategy=BackpressureStrategy.DROP_OLDEST,
            on_drop_threshold=callback,
            drop_rate_threshold=0.01,
        )

        # When — many enqueues past min-samples + drop rate well above threshold
        for i in range(500):
            buffer.put((0.0, {"i": i}))

        # Then — fired exactly once even though many drops continued
        assert callback.call_count == 1
        # And the stats payload reflects the breach
        stats_arg: RingBufferStats = callback.call_args.args[0]
        assert stats_arg.drop_rate > 0.01
        assert stats_arg.total_dropped > 0

    def test_threshold_callback_logs_warning(self, caplog):
        """``_on_drop_threshold`` emits ``dlq.outbox_drop_threshold_breached`` WARNING."""
        # Given
        import logging

        stats = RingBufferStats(
            capacity=10,
            size=10,
            total_enqueued=200,
            total_dropped=50,
            drop_rate=0.25,
        )

        # When
        with caplog.at_level(logging.WARNING):
            _on_drop_threshold(stats)

        # Then — structlog routes through std logging at WARNING
        assert any(
            "outbox_drop_threshold_breached" in rec.getMessage()
            or "outbox_drop_threshold_breached" in str(rec.__dict__)
            for rec in caplog.records
        )

    def test_threshold_callback_increments_prometheus_counter(self):
        """``dlq_outbox_drops_total`` is incremented on threshold breach."""
        # Given
        stats = RingBufferStats(
            capacity=10,
            size=10,
            total_enqueued=200,
            total_dropped=50,
            drop_rate=0.25,
        )
        mock_counter = MagicMock()

        # When
        with patch(
            "baldur.services.metrics.definitions.dlq_outbox_drops_total",
            mock_counter,
        ):
            _on_drop_threshold(stats)

        # Then
        mock_counter.labels.assert_called_with(domain="default")
        mock_counter.labels.return_value.inc.assert_called_once()

    def test_threshold_callback_emits_eventbus_event(self):
        """EventBus emits ``DLQ_OUTBOX_DROP_THRESHOLD_BREACHED`` event."""
        # Given
        stats = RingBufferStats(
            capacity=10,
            size=10,
            total_enqueued=200,
            total_dropped=50,
            drop_rate=0.25,
        )
        mock_bus = MagicMock()

        # When
        with patch(
            "baldur.services.event_bus.bus.convenience.get_event_bus",
            return_value=mock_bus,
        ):
            _on_drop_threshold(stats)

        # Then
        from baldur.services.event_bus.bus.event_types import EventType

        assert mock_bus.emit.call_count == 1
        called_args, called_kwargs = mock_bus.emit.call_args
        # First positional arg is the EventType
        assert called_args[0] == EventType.DLQ_OUTBOX_DROP_THRESHOLD_BREACHED
        # Data payload carries breach context
        assert called_kwargs["data"]["total_dropped"] == 50
        assert called_kwargs["data"]["drop_rate"] == 0.25
        assert called_kwargs["source"] == "dlq_outbox"

    def test_threshold_callback_swallows_event_emit_failure(self):
        """Event-bus failure does not propagate (fail-open)."""
        # Given
        stats = RingBufferStats(
            capacity=10,
            size=10,
            total_enqueued=200,
            total_dropped=50,
            drop_rate=0.25,
        )

        # When
        with patch(
            "baldur.services.event_bus.bus.convenience.get_event_bus",
            side_effect=RuntimeError("bus dead"),
        ):
            # Then — does not raise
            _on_drop_threshold(stats)


# =============================================================================
# Behavior — D4 leading-indicator metrics
# =============================================================================


class TestOutboxObservabilityBehavior:
    """``dlq_outbox_processing_delay_seconds`` Histogram observation."""

    def test_processing_delay_observed_with_domain_label(self):
        """Worker calls ``_on_processing_delay(delay, domain)`` per entry."""
        # Given
        mock_histogram = MagicMock()

        # When
        with patch(
            "baldur.services.metrics.definitions.dlq_outbox_processing_delay_seconds",
            mock_histogram,
        ):
            _on_processing_delay(0.05, "payment")

        # Then
        mock_histogram.labels.assert_called_with(domain="payment")
        mock_histogram.labels.return_value.observe.assert_called_with(0.05)

    def test_processing_delay_swallows_metric_failure(self):
        """Histogram registry error does not break the worker."""
        # When
        with patch(
            "baldur.services.metrics.definitions.dlq_outbox_processing_delay_seconds",
            new=MagicMock(labels=MagicMock(side_effect=RuntimeError("registry error"))),
        ):
            # Then — does not raise
            _on_processing_delay(0.1, "default")

    def test_worker_observes_delay_when_popping_entry(
        self, build_outbox, make_sync_writer, collected_writes
    ):
        """End-to-end: worker invokes ``on_processing_delay`` on pop."""
        # Given
        observed: list[tuple[float, str]] = []

        def on_delay(delay: float, domain: str) -> None:
            observed.append((delay, domain))

        writer = make_sync_writer(collected_writes)
        outbox, _, _ = build_outbox(
            writer,
            on_processing_delay=on_delay,
            batch_size=2,
            flush_interval_seconds=0.01,
        )
        outbox.start()
        try:
            # When
            outbox.put({"domain": "payment", "failure_type": "PG"})
            outbox.put({"domain": "point", "failure_type": "PT"})
            outbox.flush_and_wait(timeout=2.0)

            # Then — delay observed for each entry, with domain pulled from kwargs
            assert len(observed) == 2
            delays = sorted(d for d, _ in observed)
            domains = sorted(dn for _, dn in observed)
            assert all(d >= 0.0 for d in delays)
            assert domains == ["payment", "point"]
        finally:
            outbox.stop(timeout=1.0)

    def test_worker_uses_default_domain_label_when_kwarg_missing(
        self, build_outbox, make_sync_writer, collected_writes
    ):
        """Missing ``domain`` kwarg → label ``"default"``."""
        # Given
        observed: list[tuple[float, str]] = []
        writer = make_sync_writer(collected_writes)
        outbox, _, _ = build_outbox(
            writer,
            on_processing_delay=lambda d, dom: observed.append((d, dom)),
            batch_size=1,
            flush_interval_seconds=0.01,
        )
        outbox.start()
        try:
            # When
            outbox.put({"failure_type": "X"})  # no 'domain' key
            outbox.flush_and_wait(timeout=2.0)

            # Then
            assert len(observed) == 1
            assert observed[0][1] == "default"
        finally:
            outbox.stop(timeout=1.0)
