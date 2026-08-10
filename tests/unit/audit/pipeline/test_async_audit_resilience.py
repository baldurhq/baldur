"""
Durability tests for AsyncHealingLogger and its neighbouring components.

Covers:
- Batch flush retry policy
- Queue backpressure strategy
- Error alert thresholds
- Priority Queue ordering
- CRITICAL thread pool lifecycle
- Checkpoint configuration
- Fast serialization
"""

from __future__ import annotations

import queue

# =============================================================================
# AsyncHealingLogger Tests
# =============================================================================


class TestPriorityQueue:
    """Priority Queue ordering."""

    def test_critical_events_have_highest_priority(self):
        """A CRITICAL event outranks every other severity."""
        from baldur.utils.async_logger import (
            SEVERITY_PRIORITY_MAP,
            EventSeverity,
            LogFlushPriority,
        )

        # CRITICAL carries the lowest number, i.e. the highest priority.
        assert (
            SEVERITY_PRIORITY_MAP[EventSeverity.CRITICAL] == LogFlushPriority.CRITICAL
        )
        assert (
            SEVERITY_PRIORITY_MAP[EventSeverity.CRITICAL]
            < SEVERITY_PRIORITY_MAP[EventSeverity.WARNING]
        )
        assert (
            SEVERITY_PRIORITY_MAP[EventSeverity.WARNING]
            < SEVERITY_PRIORITY_MAP[EventSeverity.INFO]
        )
        assert (
            SEVERITY_PRIORITY_MAP[EventSeverity.INFO]
            < SEVERITY_PRIORITY_MAP[EventSeverity.DEBUG]
        )

    def test_prioritized_event_ordering(self):
        """PrioritizedEvent sorts by priority."""
        from baldur.utils.async_logger import LogFlushPriority, PrioritizedEvent

        events = [
            PrioritizedEvent(
                priority=LogFlushPriority.DEBUG, timestamp=1.0, event={"type": "debug"}
            ),
            PrioritizedEvent(
                priority=LogFlushPriority.CRITICAL,
                timestamp=2.0,
                event={"type": "critical"},
            ),
            PrioritizedEvent(
                priority=LogFlushPriority.INFO, timestamp=3.0, event={"type": "info"}
            ),
        ]

        sorted_events = sorted(events)

        assert sorted_events[0].event["type"] == "critical"
        assert sorted_events[1].event["type"] == "info"
        assert sorted_events[2].event["type"] == "debug"

    def test_priority_queue_processes_critical_first(self):
        """A PriorityQueue hands back the CRITICAL event first."""
        from baldur.utils.async_logger import LogFlushPriority, PrioritizedEvent

        pq = queue.PriorityQueue()

        # Inserted in arrival order (DEBUG, CRITICAL, INFO).
        pq.put(
            PrioritizedEvent(
                priority=LogFlushPriority.DEBUG, timestamp=1.0, event={"type": "debug"}
            )
        )
        pq.put(
            PrioritizedEvent(
                priority=LogFlushPriority.CRITICAL,
                timestamp=2.0,
                event={"type": "critical"},
            )
        )
        pq.put(
            PrioritizedEvent(
                priority=LogFlushPriority.INFO, timestamp=3.0, event={"type": "info"}
            )
        )

        # Retrieved in priority order.
        first = pq.get()
        second = pq.get()
        third = pq.get()

        assert first.event["type"] == "critical"
        assert second.event["type"] == "info"
        assert third.event["type"] == "debug"


class TestFlushRetry:
    """Batch flush retry policy."""

    def test_batch_retry_policy_defaults(self):
        """BatchRetryPolicy default values."""
        from baldur.utils.async_logger import BatchRetryPolicy

        policy = BatchRetryPolicy()

        assert policy.max_retries == 3
        assert policy.initial_delay_seconds == 1.0
        assert policy.backoff_multiplier == 2.0
        assert policy.max_delay_seconds == 30.0
        assert policy.dlq_on_final_failure is True

    def test_exponential_backoff_calculation(self):
        """Exponential backoff grows by the configured multiplier."""
        from baldur.utils.async_logger import BatchRetryPolicy

        policy = BatchRetryPolicy(
            initial_delay_seconds=1.0,
            backoff_multiplier=2.0,
            max_delay_seconds=30.0,
        )

        # attempt 0: 1.0
        # attempt 1: 2.0
        # attempt 2: 4.0
        # attempt 3: 8.0
        delays = []
        for attempt in range(4):
            delay = min(
                policy.initial_delay_seconds * (policy.backoff_multiplier**attempt),
                policy.max_delay_seconds,
            )
            delays.append(delay)

        assert delays == [1.0, 2.0, 4.0, 8.0]

    def test_max_delay_cap(self):
        """The computed delay is capped at max_delay_seconds."""
        from baldur.utils.async_logger import BatchRetryPolicy

        policy = BatchRetryPolicy(
            initial_delay_seconds=10.0,
            backoff_multiplier=3.0,
            max_delay_seconds=30.0,
        )

        # attempt 2: 10 * 3^2 = 90 -> cap to 30
        delay = min(
            policy.initial_delay_seconds * (policy.backoff_multiplier**2),
            policy.max_delay_seconds,
        )
        assert delay == 30.0


class TestQueueBackpressure:
    """Queue backpressure configuration."""

    def test_queue_overflow_policy_values(self):
        """QueueOverflowPolicy enum values."""
        from baldur.utils.async_logger import QueueOverflowPolicy

        assert QueueOverflowPolicy.DROP_NEWEST.value == "drop_newest"
        assert QueueOverflowPolicy.DROP_OLDEST.value == "drop_oldest"
        assert QueueOverflowPolicy.BLOCK.value == "block"

    def test_configure_queue_sets_values(self):
        """configure_queue applies both settings."""
        from baldur.utils.async_logger import (
            AsyncHealingLogger,
            QueueOverflowPolicy,
        )

        AsyncHealingLogger.reset()
        AsyncHealingLogger.configure_queue(
            max_size=1000,
            overflow_policy=QueueOverflowPolicy.DROP_OLDEST,
        )

        assert AsyncHealingLogger._max_queue_size == 1000
        assert AsyncHealingLogger._overflow_policy == QueueOverflowPolicy.DROP_OLDEST

        AsyncHealingLogger.reset()


class TestFlushErrorAlert:
    """Flush-error alerting configuration."""

    def test_flush_error_alert_config_defaults(self):
        """FlushErrorAlertConfig default values."""
        from baldur.utils.async_logger import FlushErrorAlertConfig

        config = FlushErrorAlertConfig()

        assert config.threshold_count == 10
        assert config.window_seconds == 60.0
        assert config.cooldown_seconds == 300.0
        assert config.severity == "CRITICAL"

    def test_configure_alert_sets_values(self):
        """configure_alert applies the supplied configuration."""
        from baldur.utils.async_logger import (
            AsyncHealingLogger,
            FlushErrorAlertConfig,
        )

        AsyncHealingLogger.reset()
        config = FlushErrorAlertConfig(
            threshold_count=5,
            window_seconds=30.0,
            cooldown_seconds=120.0,
        )
        AsyncHealingLogger.configure_alert(config)

        assert AsyncHealingLogger._alert_config.threshold_count == 5
        assert AsyncHealingLogger._alert_config.window_seconds == 30.0

        AsyncHealingLogger.reset()


class TestCriticalThreadPool:
    """CRITICAL flush thread pool lifecycle."""

    def test_critical_executor_max_workers_default(self):
        """Default worker count for the CRITICAL pool."""
        from baldur.utils.async_logger import AsyncHealingLogger

        assert AsyncHealingLogger.CRITICAL_EXECUTOR_MAX_WORKERS == 5

    def test_executor_created_on_start(self):
        """start() creates the thread pool."""
        from baldur.utils.async_logger import AsyncHealingLogger

        AsyncHealingLogger.reset()
        AsyncHealingLogger.configure(flush_callback=lambda x: None)
        AsyncHealingLogger.start()

        assert AsyncHealingLogger._critical_executor is not None

        AsyncHealingLogger.stop()
        AsyncHealingLogger.reset()

    def test_executor_shutdown_on_stop(self):
        """stop() shuts the thread pool down."""
        from baldur.utils.async_logger import AsyncHealingLogger

        AsyncHealingLogger.reset()
        AsyncHealingLogger.configure(flush_callback=lambda x: None)
        AsyncHealingLogger.start()
        AsyncHealingLogger.stop()

        assert AsyncHealingLogger._critical_executor is None

        AsyncHealingLogger.reset()


class TestAsyncLoggerStats:
    """AsyncHealingLogger statistics."""

    def test_initial_stats(self):
        """A freshly reset logger reports zeroed counters."""
        from baldur.utils.async_logger import AsyncHealingLogger

        AsyncHealingLogger.reset()
        stats = AsyncHealingLogger.get_stats()

        assert stats["events_logged"] == 0
        assert stats["events_flushed"] == 0
        assert stats["flush_errors"] == 0
        assert stats["queue_overflows"] == 0

        AsyncHealingLogger.reset()

    def test_stats_carry_no_wal_write_counter(self):
        """The logger never writes to a WAL, so it publishes no such counter.

        Regression guard for the removed WAL-first path: the counter had a
        producer and no consumer, and the surrounding write path never
        executed, so publishing it advertised durability the logger did not
        have.
        """
        from baldur.utils.async_logger import AsyncHealingLogger

        AsyncHealingLogger.reset()

        assert "wal_writes" not in AsyncHealingLogger.get_stats()

        AsyncHealingLogger.reset()

    def test_reset_stats(self):
        """reset_stats() zeroes an advanced counter."""
        from baldur.utils.async_logger import AsyncHealingLogger

        AsyncHealingLogger.reset()

        # Advance a counter by hand.
        with AsyncHealingLogger._lock:
            AsyncHealingLogger._stats["events_logged"] = 100

        AsyncHealingLogger.reset_stats()
        stats = AsyncHealingLogger.get_stats()

        assert stats["events_logged"] == 0

        AsyncHealingLogger.reset()


# =============================================================================
# SyncWorker Checkpoint Tests
# =============================================================================


class TestSyncWorkerCheckpoint:
    """SyncWorker checkpoint configuration."""

    def test_config_has_checkpoint_settings(self):
        """SyncWorkerConfig carries the checkpoint cadence settings."""
        from baldur.audit.sync_worker import SyncWorkerConfig

        config = SyncWorkerConfig()

        assert hasattr(config, "checkpoint_save_interval_batches")
        assert hasattr(config, "checkpoint_save_interval_seconds")
        assert config.checkpoint_save_interval_batches == 10
        assert config.checkpoint_save_interval_seconds == 30.0


# =============================================================================
# Serialization Tests
# =============================================================================


class TestFastSerialization:
    """Fast JSON serialization helpers."""

    def test_fast_dumps_returns_bytes(self):
        """fast_dumps returns bytes."""
        from baldur.utils.serialization import fast_dumps

        data = {"key": "value", "number": 123}
        result = fast_dumps(data)

        assert isinstance(result, bytes)

    def test_fast_loads_from_bytes(self):
        """fast_loads parses bytes."""
        from baldur.utils.serialization import fast_dumps, fast_loads

        data = {"key": "value", "number": 123}
        encoded = fast_dumps(data)
        decoded = fast_loads(encoded)

        assert decoded == data

    def test_fast_loads_from_string(self):
        """fast_loads parses a string."""
        from baldur.utils.serialization import fast_loads

        json_str = '{"key":"value","number":123}'
        decoded = fast_loads(json_str)

        assert decoded["key"] == "value"
        assert decoded["number"] == 123

    def test_fast_json_available_flag(self):
        """The FAST_JSON_AVAILABLE flag is exposed as a bool."""
        from baldur.utils.serialization import FAST_JSON_AVAILABLE

        assert isinstance(FAST_JSON_AVAILABLE, bool)

    def test_fast_dumps_str_returns_string(self):
        """fast_dumps_str returns a string."""
        from baldur.utils.serialization import fast_dumps_str

        data = {"key": "value"}
        result = fast_dumps_str(data)

        assert isinstance(result, str)
        assert "key" in result

    def test_unicode_handling(self):
        """Non-ASCII payloads survive a serialization round trip."""
        from baldur.utils.serialization import fast_dumps, fast_loads

        data = {"message": "한글 테스트", "emoji": "🎉"}
        encoded = fast_dumps(data)
        decoded = fast_loads(encoded)

        assert decoded["message"] == "한글 테스트"
        assert decoded["emoji"] == "🎉"
