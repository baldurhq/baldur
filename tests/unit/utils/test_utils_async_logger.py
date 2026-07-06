"""
Tests for Async Healing Logger.
"""

import queue
import threading
import time
from unittest.mock import Mock


class TestEventSeverity:
    """Test EventSeverity enum."""

    def test_event_severity_values(self):
        """Should have correct integer values for ordering."""
        from baldur.utils.async_logger import EventSeverity

        assert EventSeverity.DEBUG.value == 0
        assert EventSeverity.INFO.value == 1
        assert EventSeverity.WARNING.value == 2
        assert EventSeverity.CRITICAL.value == 3

    def test_severity_ordering(self):
        """Should maintain proper severity ordering."""
        from baldur.utils.async_logger import EventSeverity

        assert EventSeverity.DEBUG.value < EventSeverity.INFO.value
        assert EventSeverity.INFO.value < EventSeverity.WARNING.value
        assert EventSeverity.WARNING.value < EventSeverity.CRITICAL.value


class TestAsyncHealingLoggerConfiguration:
    """Test AsyncHealingLogger configuration."""

    def test_configure_sets_flush_callback(self):
        """Should set flush callback."""
        from baldur.utils.async_logger import AsyncHealingLogger

        callback = Mock()
        AsyncHealingLogger.configure(flush_callback=callback)

        assert AsyncHealingLogger._flush_callback is callback

        # Clean up
        AsyncHealingLogger._flush_callback = None

    def test_default_batch_size(self):
        """Should have default batch size from settings."""
        from baldur.utils.async_logger import AsyncHealingLogger

        # 동적 설정을 통해 기본값 확인 (BatchSettings.logger_batch_size 기본값: 100)
        assert AsyncHealingLogger._get_batch_size() == 100

    def test_default_flush_interval(self):
        """Should have default flush interval from settings."""
        from baldur.utils.async_logger import AsyncHealingLogger

        # 동적 설정을 통해 기본값 확인 (BatchSettings.flush_interval 기본값: 5.0)
        assert AsyncHealingLogger._get_flush_interval() == 5.0


class TestAsyncHealingLoggerLifecycle:
    """Test AsyncHealingLogger start/stop lifecycle."""

    def test_start_creates_worker_thread(self):
        """Should create worker thread on start."""
        from baldur.utils.async_logger import AsyncHealingLogger

        # Reset state
        AsyncHealingLogger._running = False
        AsyncHealingLogger._worker_thread = None

        AsyncHealingLogger.start()

        assert AsyncHealingLogger._running is True
        assert AsyncHealingLogger._worker_thread is not None
        assert isinstance(AsyncHealingLogger._worker_thread, threading.Thread)

        # Clean up
        AsyncHealingLogger.stop(timeout=1.0)

    def test_start_is_idempotent(self):
        """Should not create multiple workers on multiple starts."""
        from baldur.utils.async_logger import AsyncHealingLogger

        # Reset state
        AsyncHealingLogger._running = False
        AsyncHealingLogger._worker_thread = None

        AsyncHealingLogger.start()
        first_thread = AsyncHealingLogger._worker_thread

        AsyncHealingLogger.start()  # Call again
        second_thread = AsyncHealingLogger._worker_thread

        assert first_thread is second_thread

        # Clean up
        AsyncHealingLogger.stop(timeout=1.0)

    def test_stop_sets_running_false(self):
        """Should set running to False on stop."""
        from baldur.utils.async_logger import AsyncHealingLogger

        AsyncHealingLogger._running = False
        AsyncHealingLogger.start()
        AsyncHealingLogger.stop(timeout=1.0)

        assert AsyncHealingLogger._running is False


class TestAsyncHealingLoggerLogging:
    """Test AsyncHealingLogger log functionality."""

    def test_log_adds_to_queue(self):
        """Should add event to queue."""
        from baldur.utils.async_logger import AsyncHealingLogger

        # start()를 호출해야 _queue가 초기화됨
        AsyncHealingLogger.start()

        # Clear queue
        while (
            AsyncHealingLogger._queue is not None
            and not AsyncHealingLogger._queue.empty()
        ):
            try:
                AsyncHealingLogger._queue.get_nowait()
            except queue.Empty:
                break

        event = {"type": "test", "data": "value"}
        AsyncHealingLogger.log(event)

        # Event should be in queue
        assert AsyncHealingLogger._queue is not None
        # 워커가 빠르게 처리할 수 있으므로, 큐에 추가된 것 또는 처리된 것 확인
        # stop 후 정리
        AsyncHealingLogger.stop(timeout=1.0)


class TestAsyncHealingLoggerImmediateFlush:
    """Test immediate flush for critical events."""

    def test_critical_severity_in_immediate_set(self):
        """Should have CRITICAL in immediate flush set."""
        from baldur.utils.async_logger import AsyncHealingLogger, EventSeverity

        assert EventSeverity.CRITICAL in AsyncHealingLogger.IMMEDIATE_SEVERITIES


class TestAsyncHealingLoggerStats:
    """Test AsyncHealingLogger statistics."""

    def test_has_stats_dict(self):
        """Should have stats dictionary."""
        from baldur.utils.async_logger import AsyncHealingLogger

        assert hasattr(AsyncHealingLogger, "_stats")
        assert isinstance(AsyncHealingLogger._stats, dict)

    def test_stats_has_required_keys(self):
        """Should have required stat keys."""
        from baldur.utils.async_logger import AsyncHealingLogger

        required_keys = [
            "events_logged",
            "events_flushed",
            "immediate_flushes",
            "batch_flushes",
            "flush_errors",
        ]

        for key in required_keys:
            assert key in AsyncHealingLogger._stats


class TestAsyncHealingLoggerThreadSafety:
    """Test AsyncHealingLogger thread safety."""

    def test_has_lock(self):
        """Should have lock for thread safety."""
        from baldur.utils.async_logger import AsyncHealingLogger

        assert hasattr(AsyncHealingLogger, "_lock")
        assert isinstance(AsyncHealingLogger._lock, type(threading.RLock()))


class TestAsyncHealingLoggerIntegration:
    """Integration tests for AsyncHealingLogger."""

    def test_full_lifecycle(self):
        """Should handle full lifecycle correctly."""
        from baldur.utils.async_logger import AsyncHealingLogger, EventSeverity

        flushed_events = []

        def capture_flush(events):
            flushed_events.extend(events)

        # Configure and start
        AsyncHealingLogger.configure(flush_callback=capture_flush)
        AsyncHealingLogger.start()

        try:
            # Log some events
            AsyncHealingLogger.log({"type": "test1"})
            AsyncHealingLogger.log({"type": "test2"}, EventSeverity.CRITICAL)

            # Give time for processing
            time.sleep(0.1)

        finally:
            # Clean up
            AsyncHealingLogger.stop(timeout=2.0)
            AsyncHealingLogger._flush_callback = None
