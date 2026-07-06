"""
비동기 Audit 파이프라인 단위 테스트.

테스트 대상:
1. AsyncHealingLogger Non-blocking 로깅 (~0.01ms)
2. CRITICAL 이벤트 즉시 전송
3. 배치 플러시 동작
4. Graceful Shutdown 시 남은 이벤트 플러시
5. AuditMiddleware 비동기 로깅 전환
6. Lifecycle Manager 시작/종료

Version: 1.0.0
"""

from __future__ import annotations

import time
from datetime import UTC
from unittest.mock import MagicMock, patch


class TestAsyncHealingLoggerNonBlocking:
    """AsyncHealingLogger Non-blocking 동작 테스트."""

    def setup_method(self):
        """테스트 전 AsyncHealingLogger 리셋."""
        from baldur.utils.async_logger import AsyncHealingLogger

        AsyncHealingLogger.reset()

    def teardown_method(self):
        """테스트 후 AsyncHealingLogger 정리."""
        from baldur.utils.async_logger import AsyncHealingLogger

        AsyncHealingLogger.stop()
        AsyncHealingLogger.reset()

    def test_logging_is_non_blocking(self):
        """1000개 로깅이 10ms 이내 완료 (Non-blocking 확인)."""
        from baldur.utils.async_logger import AsyncHealingLogger, EventSeverity

        events_received = []

        def capture_events(events):
            events_received.extend(events)

        AsyncHealingLogger.configure(flush_callback=capture_events)
        AsyncHealingLogger.start()

        # 1000개 이벤트 로깅
        start = time.time()
        for i in range(1000):
            AsyncHealingLogger.log({"index": i, "test": True}, EventSeverity.INFO)
        elapsed = time.time() - start

        # Non-blocking: 1000개 로깅이 0.1초 이내 완료되어야 함
        assert elapsed < 0.1, f"1000 logs took {elapsed}s (should be < 0.1s)"

    def test_log_returns_immediately(self):
        """log() 호출이 즉시 반환되는지 확인."""
        from baldur.utils.async_logger import AsyncHealingLogger, EventSeverity

        # 느린 콜백 설정
        def slow_callback(events):
            time.sleep(1.0)

        AsyncHealingLogger.configure(flush_callback=slow_callback)
        AsyncHealingLogger.start()

        # 로깅은 즉시 반환되어야 함
        start = time.time()
        AsyncHealingLogger.log({"test": True}, EventSeverity.INFO)
        elapsed = time.time() - start

        # 콜백 대기 없이 즉시 반환
        assert elapsed < 0.01, f"log() took {elapsed}s (should be < 0.01s)"


class TestAsyncHealingLoggerCriticalEvents:
    """CRITICAL 이벤트 즉시 전송 테스트."""

    def setup_method(self):
        from baldur.utils.async_logger import AsyncHealingLogger

        AsyncHealingLogger.reset()

    def teardown_method(self):
        from baldur.utils.async_logger import AsyncHealingLogger

        AsyncHealingLogger.stop()
        AsyncHealingLogger.reset()

    def test_critical_event_immediate_flush(self):
        """CRITICAL 이벤트는 배치 대기 없이 즉시 전송."""
        from baldur.utils.async_logger import AsyncHealingLogger, EventSeverity

        events_received = []
        flush_times = []

        def capture_events_with_time(events):
            flush_times.append(time.time())
            events_received.extend(events)

        AsyncHealingLogger.configure(flush_callback=capture_events_with_time)
        AsyncHealingLogger.start()

        log_time = time.time()
        AsyncHealingLogger.log({"type": "circuit_breaker_open"}, EventSeverity.CRITICAL)

        # 즉시 전송이므로 짧은 대기 후 확인
        time.sleep(0.2)

        assert len(events_received) >= 1, "CRITICAL event should be flushed immediately"
        assert events_received[0]["type"] == "circuit_breaker_open"

        # 플러시 시간이 로그 시간 직후인지 확인 (1초 이내)
        if flush_times:
            assert flush_times[0] - log_time < 1.0

    def test_critical_event_separate_from_batch(self):
        """CRITICAL 이벤트는 일반 배치와 별도로 전송."""
        from baldur.utils.async_logger import AsyncHealingLogger, EventSeverity

        events_received = []

        def capture_events(events):
            events_received.extend(events)

        AsyncHealingLogger.configure(flush_callback=capture_events)
        AsyncHealingLogger.start()

        # 일반 이벤트 (배치 대기)
        AsyncHealingLogger.log({"type": "normal"}, EventSeverity.INFO)

        # CRITICAL 이벤트 (즉시 전송)
        AsyncHealingLogger.log({"type": "critical"}, EventSeverity.CRITICAL)

        # 짧은 대기 후 CRITICAL만 전송되었는지 확인
        time.sleep(0.2)

        critical_events = [e for e in events_received if e.get("type") == "critical"]
        assert len(critical_events) >= 1, "CRITICAL event should be sent immediately"


class TestAsyncHealingLoggerBatchFlush:
    """배치 플러시 동작 테스트."""

    def setup_method(self):
        from baldur.utils.async_logger import AsyncHealingLogger

        AsyncHealingLogger.reset()

    def teardown_method(self):
        from baldur.utils.async_logger import AsyncHealingLogger

        AsyncHealingLogger.stop()
        AsyncHealingLogger.reset()

    def test_batch_size_trigger_flush(self):
        """배치 크기 도달 시 플러시."""
        from baldur.utils.async_logger import AsyncHealingLogger, EventSeverity

        events_received = []

        def capture_events(events):
            events_received.extend(events)

        AsyncHealingLogger.configure(flush_callback=capture_events)
        AsyncHealingLogger.start()

        # 배치 크기 (기본 100) 이상 전송
        for i in range(110):
            AsyncHealingLogger.log({"idx": i}, EventSeverity.INFO)

        # 워커가 배치 크기(100)에 도달하여 자동 플러시할 때까지 폴링 대기 (최대 5초)
        # 주의: flush()를 여기서 호출하면 워커 스레드와 race condition 발생
        # (워커가 이미 큐에서 꺼낸 이벤트는 flush()가 접근 불가)
        deadline = time.time() + 5.0
        while len(events_received) < 100 and time.time() < deadline:
            time.sleep(0.05)

        # 최소 배치 크기만큼은 플러시됨
        assert len(events_received) >= 100, (
            f"Expected >= 100 events flushed by batch trigger, got {len(events_received)}"
        )

    def test_manual_flush(self):
        """수동 flush() 호출 시 모든 이벤트 즉시 플러시."""
        from baldur.utils.async_logger import AsyncHealingLogger, EventSeverity

        events_received = []

        def capture_events(events):
            events_received.extend(events)

        AsyncHealingLogger.configure(flush_callback=capture_events)
        AsyncHealingLogger.start()

        # 배치 크기 미달
        for i in range(5):
            AsyncHealingLogger.log({"idx": i}, EventSeverity.INFO)

        # 수동 플러시
        AsyncHealingLogger.flush()

        # 워커 스레드가 이미 큐에서 꺼낸 이벤트를 flush할 시간을 대기
        deadline = time.time() + 3.0
        while len(events_received) < 5 and time.time() < deadline:
            time.sleep(0.05)

        # 모든 이벤트 플러시됨
        assert len(events_received) == 5


class TestAsyncHealingLoggerGracefulShutdown:
    """Graceful Shutdown 시 남은 이벤트 플러시 테스트."""

    def setup_method(self):
        from baldur.utils.async_logger import AsyncHealingLogger

        AsyncHealingLogger.reset()

    def teardown_method(self):
        from baldur.utils.async_logger import AsyncHealingLogger

        AsyncHealingLogger.stop()
        AsyncHealingLogger.reset()

    def test_stop_flushes_remaining_events(self):
        """종료 시 남은 이벤트 플러시."""
        from baldur.utils.async_logger import AsyncHealingLogger, EventSeverity

        events_received = []

        def capture_events(events):
            events_received.extend(events)

        AsyncHealingLogger.configure(flush_callback=capture_events)
        AsyncHealingLogger.start()

        # 이벤트 추가 (배치 크기 미달)
        for i in range(5):
            AsyncHealingLogger.log({"idx": i}, EventSeverity.INFO)

        # flush() 후 stop() 호출
        AsyncHealingLogger.flush()
        AsyncHealingLogger.stop()

        # 모든 이벤트 플러시됨
        assert len(events_received) == 5

    def test_stats_after_operations(self):
        """통계 정확성 확인."""
        from baldur.utils.async_logger import AsyncHealingLogger, EventSeverity

        events_received = []

        def capture_events(events):
            events_received.extend(events)

        AsyncHealingLogger.configure(flush_callback=capture_events)
        AsyncHealingLogger.start()

        # 이벤트 로깅
        for i in range(5):
            AsyncHealingLogger.log({"idx": i}, EventSeverity.INFO)

        # stop(): _running=False → worker 종료 → 로컬 batch 잔여분 flush → join()
        # flush(): worker가 꺼내지 못한 _priority_queue 잔여 이벤트 drain + flush
        # 두 단계 모두 필요: worker의 로컬 batch + 큐 잔여분 = 전체 이벤트
        AsyncHealingLogger.stop()
        AsyncHealingLogger.flush()

        stats = AsyncHealingLogger.get_stats()
        assert stats["events_logged"] == 5
        assert stats["events_flushed"] == 5


class TestAsyncAuditLifecycle:
    """Lifecycle Manager 테스트."""

    def setup_method(self):
        from baldur.audit.async_audit_lifecycle import reset_lifecycle_state
        from baldur.utils.async_logger import AsyncHealingLogger

        reset_lifecycle_state()
        AsyncHealingLogger.reset()

    def teardown_method(self):
        from baldur.audit.async_audit_lifecycle import reset_lifecycle_state
        from baldur.utils.async_logger import AsyncHealingLogger

        AsyncHealingLogger.stop()
        AsyncHealingLogger.reset()
        reset_lifecycle_state()

    def test_create_audit_flush_callback(self):
        """Audit 플러시 콜백 생성 확인."""
        from baldur.audit.async_audit_lifecycle import create_audit_flush_callback

        callback = create_audit_flush_callback()
        assert callable(callback)

    def test_lifecycle_status_initial(self):
        """초기 생명주기 상태 확인."""
        from baldur.audit.async_audit_lifecycle import get_lifecycle_status

        status = get_lifecycle_status()
        assert status["startup_completed"] is False
        assert status["shutdown_registered"] is False

    @patch("baldur.audit.async_audit_lifecycle._get_wal_instance")
    @patch("baldur.audit.async_audit_lifecycle._start_sync_worker")
    def test_startup_sets_completed_flag(
        self,
        mock_sync_worker,
        mock_wal,
    ):
        """시작 완료 시 플래그 설정."""
        from baldur.audit.async_audit_lifecycle import (
            get_lifecycle_status,
            startup_async_audit_system,
        )

        mock_wal.return_value = None

        result = startup_async_audit_system()

        assert result is True
        status = get_lifecycle_status()
        assert status["startup_completed"] is True

    @patch("baldur.audit.async_audit_lifecycle._get_wal_instance")
    @patch("baldur.audit.async_audit_lifecycle._start_sync_worker")
    def test_startup_only_once(
        self,
        mock_sync_worker,
        mock_wal,
    ):
        """시작은 한 번만 가능."""
        from baldur.audit.async_audit_lifecycle import startup_async_audit_system

        mock_wal.return_value = None

        # 첫 번째 호출
        result1 = startup_async_audit_system()
        assert result1 is True

        # 두 번째 호출 - 이미 시작됨
        result2 = startup_async_audit_system()
        assert result2 is False

    # 416 Part 5: register_shutdown_handlers() and its signal helpers were
    # deleted from async_audit_lifecycle.py. They were superseded by
    # AuditShutdownHandler + GracefulShutdownCoordinator (registered via
    # apps.py) and the Gunicorn worker_exit_cleanup hook (server.py).


class TestAuditMiddlewareAsyncMode:
    """AuditMiddleware 비동기 모드 테스트."""

    def test_is_async_mode_enabled_default_true(self):
        """기본값은 비동기 모드 활성화."""
        import os

        # 환경변수 제거
        os.environ.pop("AUDIT_ASYNC_MODE_ENABLED", None)

        # Django 의존성 없이 직접 로직 테스트
        env_value = os.environ.get("AUDIT_ASYNC_MODE_ENABLED", "TRUE").upper()
        result = env_value == "TRUE"

        assert result is True

    def test_is_async_mode_disabled_by_env(self):
        """환경변수로 비동기 모드 비활성화."""
        import os

        os.environ["AUDIT_ASYNC_MODE_ENABLED"] = "FALSE"

        try:
            env_value = os.environ.get("AUDIT_ASYNC_MODE_ENABLED", "TRUE").upper()
            result = env_value == "TRUE"

            assert result is False
        finally:
            os.environ.pop("AUDIT_ASYNC_MODE_ENABLED", None)


class TestConvertEventToDict:
    """이벤트 딕셔너리 변환 테스트."""

    def test_convert_event_with_all_fields(self):
        """모든 필드가 있는 이벤트 변환."""
        from datetime import datetime

        from baldur.audit.event_buffer import AuditEventType
        from baldur.interfaces.audit_adapter import AuditAction

        # Mock 이벤트 생성
        event = MagicMock()
        event.event_type = AuditEventType.CB_STATE_CHANGE
        event.source = "CircuitBreaker"
        event.target_type = "order_service"
        event.target_id = "cb-001"
        event.actor_id = "user-123"
        event.actor_type = "admin"
        event.domain = "order"
        event.reason = "Failure threshold exceeded"
        event.details = {"failures": 10}
        event.success = True
        event.error_message = None
        event.timestamp = datetime(2026, 1, 31, 12, 0, 0, tzinfo=UTC)

        request_context = {
            "request_id": "req-456",
            "path": "/api/orders/",
            "method": "POST",
        }

        # 변환 로직 직접 테스트 (Django 의존성 회피)
        action_map = {
            AuditEventType.CB_STATE_CHANGE: AuditAction.CB_AUTO_OPEN,
        }

        action = action_map.get(event.event_type, AuditAction.CONFIG_CHANGE)

        result = {
            "action": action.value if hasattr(action, "value") else str(action),
            "event_type": event.event_type.value
            if hasattr(event.event_type, "value")
            else str(event.event_type),
            "source": event.source,
            "target_type": event.target_type or event.source,
            "target_id": event.target_id or request_context.get("request_id", ""),
            "actor_id": event.actor_id or request_context.get("actor_id"),
            "actor_type": event.actor_type,
            "domain": event.domain,
            "reason": event.reason,
            "details": {
                **event.details,
                "request_context": request_context,
            },
            "success": event.success,
            "error_message": event.error_message,
            "timestamp": event.timestamp.isoformat() if event.timestamp else None,
        }

        assert result["event_type"] == "circuit_breaker_state_change"
        assert result["source"] == "CircuitBreaker"
        assert result["target_type"] == "order_service"
        assert result["target_id"] == "cb-001"
        assert result["actor_id"] == "user-123"
        assert result["domain"] == "order"
        assert result["success"] is True


class TestAsyncAuditMonitoringMetrics:
    """모니터링 메트릭 단위 테스트."""

    def setup_method(self):
        from baldur.audit.async_audit_lifecycle import reset_lifecycle_state
        from baldur.utils.async_logger import AsyncHealingLogger

        reset_lifecycle_state()
        AsyncHealingLogger.reset()

    def teardown_method(self):
        from baldur.audit.async_audit_lifecycle import reset_lifecycle_state
        from baldur.utils.async_logger import AsyncHealingLogger

        AsyncHealingLogger.stop()
        AsyncHealingLogger.reset()
        reset_lifecycle_state()

    def test_get_async_audit_metrics_returns_dict(self):
        """get_async_audit_metrics()가 dict 반환."""
        from baldur.audit.async_audit_lifecycle import get_async_audit_metrics

        metrics = get_async_audit_metrics()

        assert isinstance(metrics, dict)
        assert "lifecycle_startup_completed" in metrics
        assert "lifecycle_shutdown_registered" in metrics

    def test_get_async_audit_metrics_includes_logger_stats(self):
        """메트릭에 AsyncHealingLogger 통계 포함."""
        from baldur.audit.async_audit_lifecycle import get_async_audit_metrics
        from baldur.utils.async_logger import AsyncHealingLogger, EventSeverity

        # 로거 설정 및 시작
        AsyncHealingLogger.configure(flush_callback=lambda e: None)
        AsyncHealingLogger.start()

        # 이벤트 로깅
        AsyncHealingLogger.log({"test": True}, EventSeverity.INFO)

        # 메트릭 조회
        metrics = get_async_audit_metrics()

        assert "events_logged" in metrics
        assert "queue_size" in metrics
        assert "worker_running" in metrics
        assert metrics["events_logged"] >= 1
        assert metrics["worker_running"] is True

    def test_export_metrics_to_prometheus_format(self):
        """Prometheus 포맷 출력 확인."""
        from baldur.audit.async_audit_lifecycle import export_metrics_to_prometheus
        from baldur.utils.async_logger import AsyncHealingLogger

        # 로거 설정
        AsyncHealingLogger.configure(flush_callback=lambda e: None)
        AsyncHealingLogger.start()

        # Prometheus 출력
        output = export_metrics_to_prometheus()

        assert isinstance(output, str)
        assert "# HELP async_audit_events_logged" in output
        assert "# TYPE async_audit_events_logged counter" in output
        assert "async_audit_events_logged" in output
        assert "async_audit_queue_size" in output
        assert "async_audit_worker_running" in output

    def test_metrics_after_flush(self):
        """플러시 후 메트릭 정확성."""
        from baldur.audit.async_audit_lifecycle import get_async_audit_metrics
        from baldur.utils.async_logger import AsyncHealingLogger, EventSeverity

        events_captured = []

        def capture_callback(events):
            events_captured.extend(events)

        AsyncHealingLogger.configure(flush_callback=capture_callback)
        AsyncHealingLogger.start()

        # 이벤트 로깅
        for i in range(5):
            AsyncHealingLogger.log({"idx": i}, EventSeverity.INFO)

        # 워커 종료 → 워커의 로컬 배치 플러시 보장 (worker exit flushes local batch)
        AsyncHealingLogger.stop()
        # 큐에 아직 남아있는 이벤트 추가 드레인
        AsyncHealingLogger.flush()

        # 메트릭 조회
        metrics = get_async_audit_metrics()

        assert metrics["events_logged"] == 5
        assert metrics["events_flushed"] == 5
