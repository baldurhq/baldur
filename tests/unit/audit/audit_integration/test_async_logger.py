"""
AsyncLoggerAdapter Tests.

비동기 로거 어댑터 테스트.
Uses lazy imports to avoid Prometheus registry conflicts.
"""

import threading
import time


class TestAsyncLoggerAdapter:
    """AsyncLoggerAdapter 테스트."""

    def test_init_default_config(self):
        """기본 설정으로 초기화."""
        from baldur.audit.audit_integration import AsyncLoggerAdapter

        adapter = AsyncLoggerAdapter()

        assert adapter._config.batch_size == 5
        assert adapter._config.flush_interval_seconds == 2.0
        assert adapter._config.max_queue_size == 5000
        assert not adapter._running

    def test_init_custom_config(self):
        """커스텀 설정으로 초기화."""
        from baldur.audit.audit_integration import (
            AsyncLoggerAdapter,
            AsyncLoggerConfig,
        )

        config = AsyncLoggerConfig(
            batch_size=10,
            flush_interval_seconds=1.0,
            max_queue_size=1000,
        )
        adapter = AsyncLoggerAdapter(config=config)

        assert adapter._config.batch_size == 10
        assert adapter._config.flush_interval_seconds == 1.0
        assert adapter._config.max_queue_size == 1000

    def test_start_stop(self):
        """워커 시작/중지."""
        from baldur.audit.audit_integration import AsyncLoggerAdapter

        adapter = AsyncLoggerAdapter()

        # 시작
        adapter.start()
        assert adapter._running
        assert adapter._worker_thread is not None
        assert adapter._worker_thread.is_alive()

        # 중복 시작 무시
        adapter.start()
        assert adapter._running

        # 중지
        adapter.stop()
        assert not adapter._running

    def test_log_info_event(self):
        """INFO 이벤트 로깅 (큐에 추가)."""
        from baldur.audit.audit_integration import (
            AsyncLoggerAdapter,
            EventSeverity,
        )

        adapter = AsyncLoggerAdapter()

        result = adapter.log({"action": "test"}, EventSeverity.INFO)

        assert result is True
        assert adapter._queue.qsize() == 1
        assert adapter._stats["events_logged"] == 1

    def test_log_critical_event_immediate_flush(self):
        """CRITICAL 이벤트 즉시 전송."""
        from baldur.audit.audit_integration import (
            AsyncLoggerAdapter,
            EventSeverity,
        )

        flushed_events = []

        def callback(events):
            flushed_events.extend(events)

        adapter = AsyncLoggerAdapter(flush_callback=callback)

        adapter.log({"action": "critical_test"}, EventSeverity.CRITICAL)

        # 즉시 전송 (비동기이므로 잠시 대기)
        time.sleep(0.1)

        assert adapter._stats["immediate_flushes"] == 1
        assert len(flushed_events) == 1
        assert flushed_events[0]["action"] == "critical_test"

    def test_log_warning_event_immediate_flush(self):
        """WARNING 이벤트 즉시 전송."""
        from baldur.audit.audit_integration import (
            AsyncLoggerAdapter,
            EventSeverity,
        )

        flushed_events = []

        def callback(events):
            flushed_events.extend(events)

        adapter = AsyncLoggerAdapter(flush_callback=callback)

        adapter.log({"action": "warning_test"}, EventSeverity.WARNING)

        time.sleep(0.1)

        assert adapter._stats["immediate_flushes"] == 1
        assert len(flushed_events) == 1

    def test_log_cb_event(self):
        """Circuit Breaker 이벤트 헬퍼."""
        from baldur.audit.audit_integration import AsyncLoggerAdapter

        flushed_events = []

        def callback(events):
            flushed_events.extend(events)

        adapter = AsyncLoggerAdapter(flush_callback=callback)

        # OPEN 상태는 CRITICAL
        adapter.log_cb_event(
            service="test_service", state="OPEN", reason="too many failures"
        )

        time.sleep(0.1)

        assert len(flushed_events) == 1
        assert flushed_events[0]["type"] == "circuit_breaker"
        assert flushed_events[0]["service"] == "test_service"
        assert flushed_events[0]["state"] == "OPEN"

    def test_log_recovery_event(self):
        """복구 이벤트 헬퍼."""
        from baldur.audit.audit_integration import AsyncLoggerAdapter

        adapter = AsyncLoggerAdapter()

        adapter.log_recovery_event(
            service="test_service",
            recovery_time_ms=150.5,
            success=True,
        )

        assert adapter._queue.qsize() == 1
        event = adapter._queue.get_nowait()
        assert event["type"] == "recovery"
        assert event["recovery_time_ms"] == 150.5

    def test_log_emergency_event(self):
        """Emergency 이벤트 헬퍼."""
        from baldur.audit.audit_integration import AsyncLoggerAdapter

        flushed_events = []

        def callback(events):
            flushed_events.extend(events)

        adapter = AsyncLoggerAdapter(flush_callback=callback)

        adapter.log_emergency_event(
            level="CRITICAL",
            action="trigger",
            reason="all_backends_failed",
        )

        time.sleep(0.1)

        assert len(flushed_events) == 1
        assert flushed_events[0]["type"] == "emergency"

    def test_log_fallback_activated(self):
        """Fallback 활성화 이벤트 헬퍼."""
        from baldur.audit.audit_integration import AsyncLoggerAdapter

        flushed_events = []

        def callback(events):
            flushed_events.extend(events)

        adapter = AsyncLoggerAdapter(flush_callback=callback)

        adapter.log_fallback_activated(
            fallback_type="file",
            reason="primary_failed",
        )

        time.sleep(0.1)

        assert len(flushed_events) == 1
        assert flushed_events[0]["type"] == "fallback_activated"
        assert flushed_events[0]["fallback_type"] == "file"

    def test_log_audit_event(self):
        """감사 이벤트 헬퍼."""
        from baldur.audit.audit_integration import AsyncLoggerAdapter

        adapter = AsyncLoggerAdapter()

        adapter.log_audit_event(
            action="auto_tuning",
            success=True,
            audit_id="audit-123",
        )

        assert adapter._queue.qsize() == 1
        event = adapter._queue.get_nowait()
        assert event["type"] == "audit"
        assert event["audit_id"] == "audit-123"

    def test_queue_overflow_drops_event(self):
        """큐 오버플로우 시 이벤트 드롭."""
        from baldur.audit.audit_integration import (
            AsyncLoggerAdapter,
            AsyncLoggerConfig,
            EventSeverity,
        )

        config = AsyncLoggerConfig(max_queue_size=2)
        adapter = AsyncLoggerAdapter(config=config)

        # 큐 채우기
        adapter.log({"id": 1}, EventSeverity.INFO)
        adapter.log({"id": 2}, EventSeverity.INFO)

        # 세 번째는 드롭됨
        result = adapter.log({"id": 3}, EventSeverity.INFO)

        assert result is False
        assert adapter._stats["queue_overflows"] == 1

    def test_batch_flush(self):
        """배치 플러시 테스트."""
        from baldur.audit.audit_integration import (
            AsyncLoggerAdapter,
            AsyncLoggerConfig,
            EventSeverity,
        )

        flushed_events = []

        def callback(events):
            flushed_events.extend(events)

        config = AsyncLoggerConfig(batch_size=3, flush_interval_seconds=0.1)
        adapter = AsyncLoggerAdapter(flush_callback=callback, config=config)
        adapter.start()

        try:
            # 3개 이벤트 추가 (배치 크기 도달)
            adapter.log({"id": 1}, EventSeverity.INFO)
            adapter.log({"id": 2}, EventSeverity.INFO)
            adapter.log({"id": 3}, EventSeverity.INFO)

            # 배치 플러시 대기
            time.sleep(0.3)

            assert len(flushed_events) >= 3
        finally:
            adapter.stop()

    def test_flush_now(self):
        """수동 플러시."""
        from baldur.audit.audit_integration import (
            AsyncLoggerAdapter,
            EventSeverity,
        )

        flushed_events = []

        def callback(events):
            flushed_events.extend(events)

        adapter = AsyncLoggerAdapter(flush_callback=callback)

        # 이벤트 추가
        adapter.log({"id": 1}, EventSeverity.INFO)
        adapter.log({"id": 2}, EventSeverity.INFO)

        # 수동 플러시
        count = adapter.flush_now()

        assert count == 2
        assert len(flushed_events) == 2

    def test_get_stats(self):
        """통계 조회."""
        from baldur.audit.audit_integration import (
            AsyncLoggerAdapter,
            EventSeverity,
        )

        adapter = AsyncLoggerAdapter()
        adapter.start()

        try:
            adapter.log({"test": 1}, EventSeverity.INFO)

            stats = adapter.get_stats()

            assert stats["events_logged"] == 1
            assert stats["is_running"] is True
        finally:
            adapter.stop()

    def test_reset_stats(self):
        """통계 초기화."""
        from baldur.audit.audit_integration import (
            AsyncLoggerAdapter,
            EventSeverity,
        )

        adapter = AsyncLoggerAdapter()
        adapter.log({"test": 1}, EventSeverity.INFO)

        adapter.reset_stats()

        assert adapter._stats["events_logged"] == 0

    def test_configure_runtime(self):
        """런타임 설정 변경."""
        from unittest.mock import Mock

        from baldur.audit.audit_integration import AsyncLoggerAdapter

        adapter = AsyncLoggerAdapter()

        new_callback = Mock()
        adapter.configure(
            flush_callback=new_callback,
            batch_size=20,
            flush_interval=5.0,
        )

        assert adapter._flush_callback == new_callback
        assert adapter._config.batch_size == 20
        assert adapter._config.flush_interval_seconds == 5.0

    def test_thread_safety(self):
        """스레드 안전성 테스트."""
        from baldur.audit.audit_integration import (
            AsyncLoggerAdapter,
            EventSeverity,
        )

        flushed_events = []
        lock = threading.Lock()

        def callback(events):
            with lock:
                flushed_events.extend(events)

        adapter = AsyncLoggerAdapter(flush_callback=callback)
        adapter.start()

        try:
            # 여러 스레드에서 동시 로깅
            def log_events(start_id):
                for i in range(100):
                    adapter.log({"id": start_id + i}, EventSeverity.INFO)

            threads = [
                threading.Thread(target=log_events, args=(i * 100,)) for i in range(5)
            ]

            for t in threads:
                t.start()
            for t in threads:
                t.join()

            # 플러시
            adapter.flush_now()
            time.sleep(0.5)

            # 이벤트 손실 없이 처리되어야 함
            assert adapter._stats["events_logged"] == 500
        finally:
            adapter.stop()
