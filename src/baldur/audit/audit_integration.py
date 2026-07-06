"""
Audit Integration Module.

통합 연결 모듈:
1. ContinuousAuditRecorder ↔ CircuitBreaker 강화 연결
2. ContinuousAuditRecorder ↔ SyslogFallback 자동 연결
3. AsyncLogger ↔ ContinuousAudit 어댑터

Design:
    - Adapter 패턴: 기존 코드 수정 없이 연결
    - Observer 패턴: 이벤트 발생 시 양쪽에 전파
    - Non-blocking: 메인 애플리케이션 성능 영향 없음

Usage:
    from baldur.audit.audit_integration import (
        IntegratedAuditRecorder,
        AsyncLoggerAdapter,
        configure_integration,
    )

    # 통합 설정
    recorder = IntegratedAuditRecorder(adapter)

    # 또는 기존 ResilientRecorder에 AsyncLogger 연결
    async_adapter = AsyncLoggerAdapter(flush_callback=send_to_command_center)
    recorder.attach_async_logger(async_adapter)
"""

from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, Any

import structlog

from baldur.interfaces.audit_adapter import AuditEntry

if TYPE_CHECKING:
    from baldur.audit.resilient_recorder import ResilientContinuousAuditRecorder
    from baldur.settings.batch import BatchSettings

logger = structlog.get_logger()


# =============================================================================
# Event Severity - 단일 소스는 utils/async_logger.py (Item 4 중복 제거)
# =============================================================================

from baldur.utils.async_logger import EventSeverity  # noqa: E402, F401
from baldur.utils.time import utc_now

# =============================================================================
# AsyncLogger Adapter
# =============================================================================


@dataclass
class AsyncLoggerConfig:
    """AsyncLogger 어댑터 설정."""

    batch_size: int = 5
    flush_interval_seconds: float = 2.0
    max_queue_size: int = 5000
    immediate_severities: set[EventSeverity] = field(
        default_factory=lambda: {EventSeverity.CRITICAL, EventSeverity.WARNING}
    )

    @classmethod
    def from_settings(
        cls,
        settings: BatchSettings | None = None,
        **overrides,
    ) -> AsyncLoggerConfig:
        """
        Settings에서 AsyncLoggerConfig 인스턴스 생성.

        Args:
            settings: BatchSettings 인스턴스 (없으면 싱글톤 사용)
            **overrides: 개별 필드 오버라이드

        Returns:
            AsyncLoggerConfig: Settings 기반 인스턴스
        """
        from baldur.settings.batch import get_batch_settings

        s = settings or get_batch_settings()
        return cls(
            batch_size=overrides.get("batch_size", s.async_logger_batch_size),
            flush_interval_seconds=overrides.get(
                "flush_interval_seconds", s.async_logger_flush_interval
            ),
            max_queue_size=overrides.get(
                "max_queue_size", s.async_logger_max_queue_size
            ),
            immediate_severities=overrides.get(
                "immediate_severities",
                {EventSeverity.CRITICAL, EventSeverity.WARNING},
            ),
        )


class AsyncLoggerAdapter:
    """
    AsyncHealingLogger 호환 어댑터.

    load_tests/utils/baldur/async_logger.py의 AsyncHealingLogger와
    동일한 인터페이스를 제공하면서, baldur 패키지 내에서 독립 사용 가능.

    주요 기능:
    1. 비동기 이벤트 버퍼링 (Non-blocking)
    2. CRITICAL/WARNING 이벤트 즉시 전송
    3. 배치 처리로 네트워크 효율화
    4. Thread-safe 동작
    """

    def __init__(
        self,
        flush_callback: Callable[[list[dict[str, Any]]], None] | None = None,
        config: AsyncLoggerConfig | None = None,
    ):
        """
        Initialize AsyncLoggerAdapter.

        Args:
            flush_callback: 배치 이벤트를 받아 전송하는 함수
            config: 어댑터 설정
        """
        self._flush_callback = flush_callback
        self._config = config or AsyncLoggerConfig()

        # Queue
        self._queue: queue.Queue = queue.Queue(maxsize=self._config.max_queue_size)

        # Thread control
        self._running = False
        self._worker_thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._handle: Any | None = None  # DaemonWorkerHandle (impl 489 D9)

        # Statistics
        self._stats = {
            "events_logged": 0,
            "events_flushed": 0,
            "immediate_flushes": 0,
            "batch_flushes": 0,
            "flush_errors": 0,
            "queue_overflows": 0,
        }

    def configure(
        self,
        flush_callback: Callable[[list[dict[str, Any]]], None],
        batch_size: int = 5,
        flush_interval: float = 2.0,
        max_queue_size: int = 5000,
    ) -> None:
        """
        런타임 설정 변경.

        Args:
            flush_callback: 배치 전송 콜백
            batch_size: 배치 크기
            flush_interval: 플러시 간격 (초)
            max_queue_size: 최대 큐 크기
        """
        with self._lock:
            self._flush_callback = flush_callback
            self._config.batch_size = batch_size
            self._config.flush_interval_seconds = flush_interval
            # 큐 크기는 런타임 변경 불가 (재생성 필요)

    def start(self) -> None:
        """백그라운드 워커 시작."""
        from baldur.meta.daemon_worker import DaemonWorkerHandle
        from baldur.metrics.recorders.daemon_worker import register_daemon_worker

        with self._lock:
            if self._running:
                return

            self._running = True
            self._spawn_thread()
            assert self._worker_thread is not None  # _spawn_thread() invariant
            self._handle = DaemonWorkerHandle(
                thread=self._worker_thread,
                tick_interval_seconds=self._config.flush_interval_seconds,
                restart_callback=self._spawn_thread,
            )
            register_daemon_worker("AsyncLoggerAdapter", self._handle)
            logger.info("async_logger_adapter.worker_started")

    def _spawn_thread(self) -> None:
        """Construct + start a fresh worker thread (impl 489 D9)."""
        self._worker_thread = threading.Thread(
            target=self._worker_loop_with_crash_capture,
            daemon=True,
            name="AsyncLoggerAdapter",
        )
        self._worker_thread.start()
        if self._handle is not None:
            self._handle.thread = self._worker_thread

    def _worker_loop_with_crash_capture(self) -> None:
        try:
            self._worker_loop()
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as e:
            if self._handle is not None:
                self._handle.record_crash(e)
            raise

    def stop(self, timeout: float | None = None) -> None:
        """백그라운드 워커 중지."""
        from baldur.metrics.recorders.daemon_worker import unregister_daemon_worker

        if timeout is None:
            from baldur.settings.thread_management import (
                get_thread_management_settings,
            )

            timeout = get_thread_management_settings().join_timeout
        with self._lock:
            if not self._running:
                return
            if self._handle is not None:
                self._handle.is_stopping = True
            self._running = False

        if self._worker_thread:
            self._worker_thread.join(timeout=timeout)

        unregister_daemon_worker("AsyncLoggerAdapter")
        if self._worker_thread is not None and self._worker_thread.is_alive():
            logger.critical(
                "daemon_worker.stop_join_timeout",
                worker_name="AsyncLoggerAdapter",
                join_timeout_seconds=timeout,
            )
        logger.info("async_logger_adapter.worker_stopped")

    def log(
        self,
        event: dict[str, Any],
        severity: EventSeverity = EventSeverity.INFO,
    ) -> bool:
        """
        이벤트 로깅 (Non-blocking, ~0.01ms).

        Args:
            event: 이벤트 딕셔너리
            severity: 이벤트 심각도

        Returns:
            True if queued/sent, False if dropped
        """
        enriched_event = {
            **event,
            "severity": severity.name,
            "timestamp": time.time(),
            "timestamp_iso": utc_now().isoformat(),
        }

        self._stats["events_logged"] += 1

        if severity in self._config.immediate_severities:
            # CRITICAL/WARNING: 즉시 전송 (별도 스레드)
            self._stats["immediate_flushes"] += 1
            threading.Thread(
                target=self._flush_immediate,
                args=([enriched_event],),
                daemon=True,
                name="ImmediateFlush",
            ).start()
            return True
        # 일반: 배치 대기
        try:
            self._queue.put_nowait(enriched_event)
            return True
        except queue.Full:
            self._stats["queue_overflows"] += 1
            logger.warning("async_logger_adapter.queue_full_event_dropped")
            return False

    def log_cb_event(
        self,
        service: str,
        state: str,
        reason: str = "",
        **kwargs,
    ) -> None:
        """Circuit Breaker 이벤트 로깅."""
        severity = (
            EventSeverity.CRITICAL
            if state in ["OPEN", "BLOCKED"]
            else EventSeverity.INFO
        )
        self.log(
            {
                "type": "circuit_breaker",
                "service": service,
                "state": state,
                "reason": reason,
                **kwargs,
            },
            severity,
        )

    def log_recovery_event(
        self,
        service: str,
        recovery_time_ms: float,
        success: bool = True,
        **kwargs,
    ) -> None:
        """복구 이벤트 로깅."""
        self.log(
            {
                "type": "recovery",
                "service": service,
                "recovery_time_ms": recovery_time_ms,
                "success": success,
                **kwargs,
            },
            EventSeverity.INFO,
        )

    def log_emergency_event(
        self,
        level: str,
        action: str,
        reason: str = "",
        **kwargs,
    ) -> None:
        """Emergency/Fallback 이벤트 로깅."""
        severity = (
            EventSeverity.CRITICAL if action == "trigger" else EventSeverity.WARNING
        )
        self.log(
            {
                "type": "emergency",
                "level": level,
                "action": action,
                "reason": reason,
                **kwargs,
            },
            severity,
        )

    def log_fallback_activated(
        self,
        fallback_type: str,
        reason: str,
        **kwargs,
    ) -> None:
        """Fallback 활성화 이벤트 로깅."""
        self.log(
            {
                "type": "fallback_activated",
                "fallback_type": fallback_type,
                "reason": reason,
                **kwargs,
            },
            EventSeverity.WARNING,
        )

    def log_audit_event(
        self,
        action: str,
        success: bool,
        audit_id: str = "",
        **kwargs,
    ) -> None:
        """감사 이벤트 로깅 (ContinuousAudit 연동용)."""
        severity = EventSeverity.INFO if success else EventSeverity.WARNING
        self.log(
            {
                "type": "audit",
                "action": action,
                "success": success,
                "audit_id": audit_id,
                **kwargs,
            },
            severity,
        )

    def flush_now(self) -> int:
        """수동으로 즉시 플러시."""
        batch = []
        while not self._queue.empty():
            try:
                batch.append(self._queue.get_nowait())
            except queue.Empty:
                break

        if batch:
            self._flush_batch(batch)
        return len(batch)

    def get_stats(self) -> dict[str, Any]:
        """통계 반환."""
        return {
            **self._stats,
            "queue_size": self._queue.qsize(),
            "is_running": self._running,
        }

    def reset_stats(self) -> None:
        """통계 초기화."""
        self._stats = {
            "events_logged": 0,
            "events_flushed": 0,
            "immediate_flushes": 0,
            "batch_flushes": 0,
            "flush_errors": 0,
            "queue_overflows": 0,
        }

    def _worker_loop(self) -> None:
        """배치 처리 워커."""
        batch: list[dict[str, Any]] = []
        last_flush = time.time()

        while self._running:
            iter_start = time.monotonic()
            try:
                event = self._queue.get(timeout=1.0)
                batch.append(event)
            except queue.Empty:
                pass

            # 배치 사이즈 도달 또는 시간 경과 시 전송
            should_flush = len(batch) >= self._config.batch_size or (
                batch
                and time.time() - last_flush >= self._config.flush_interval_seconds
            )

            if should_flush:
                self._flush_batch(batch)
                batch = []
                last_flush = time.time()

            if self._handle is not None:
                self._handle.observe_iteration(time.monotonic() - iter_start)
                self._handle.heartbeat()

        # 종료 시 남은 이벤트 처리
        if batch:
            self._flush_batch(batch)

        # 큐에 남은 이벤트도 처리
        remaining = []
        while not self._queue.empty():
            try:
                remaining.append(self._queue.get_nowait())
            except queue.Empty:
                break
        if remaining:
            self._flush_batch(remaining)

    def _flush_batch(self, events: list[dict[str, Any]]) -> None:
        """배치 전송."""
        if not self._flush_callback or not events:
            return

        try:
            self._flush_callback(events)
            self._stats["events_flushed"] += len(events)
            self._stats["batch_flushes"] += 1
            logger.debug(
                "async_logger_adapter.flushed_events",
                events_count=len(events),
            )
        except Exception as e:
            self._stats["flush_errors"] += 1
            logger.exception(
                "async_logger_adapter.flush_failed",
                error=e,
            )

    def _flush_immediate(self, events: list[dict[str, Any]]) -> None:
        """즉시 전송."""
        self._flush_batch(events)


# =============================================================================
# Audit Event Observer
# =============================================================================


class AuditObserverEventType(str, Enum):
    """감사 이벤트 유형."""

    # Record events
    RECORD_SUCCESS = "record_success"
    RECORD_FAILED = "record_failed"

    # Circuit breaker events
    CIRCUIT_OPENED = "circuit_opened"
    CIRCUIT_CLOSED = "circuit_closed"
    CIRCUIT_HALF_OPEN = "circuit_half_open"

    # Fallback events
    FALLBACK_ACTIVATED = "fallback_activated"
    FALLBACK_FAILED = "fallback_failed"
    SYSLOG_ACTIVATED = "syslog_activated"

    # Recovery events
    PRIMARY_RECOVERED = "primary_recovered"

    # Health events
    BUFFER_OVERFLOW = "buffer_overflow"
    DEGRADED_MODE_ENTERED = "degraded_mode_entered"
    DEGRADED_MODE_EXITED = "degraded_mode_exited"


@dataclass
class AuditEventData:
    """감사 이벤트 데이터."""

    event_type: AuditObserverEventType
    timestamp: datetime = field(default_factory=lambda: utc_now())
    details: dict[str, Any] = field(default_factory=dict)


class AuditEventObserver:
    """
    감사 이벤트 옵저버 인터페이스.

    Observer 패턴으로 감사 시스템 이벤트를 외부에 전파.
    """

    def on_event(self, event: AuditEventData) -> None:
        """이벤트 수신 시 호출."""
        raise NotImplementedError


class AsyncLoggerObserver(AuditEventObserver):
    """
    AsyncLoggerAdapter를 Observer로 래핑.

    감사 이벤트를 AsyncLogger 이벤트로 변환하여 전송.
    """

    def __init__(self, async_logger: AsyncLoggerAdapter):
        self._async_logger = async_logger

    def on_event(self, event: AuditEventData) -> None:
        """감사 이벤트를 AsyncLogger로 전파."""
        event_type = event.event_type

        # 이벤트 유형별 변환
        if event_type == AuditObserverEventType.CIRCUIT_OPENED:
            self._async_logger.log_cb_event(
                service=event.details.get("service", "audit_primary"),
                state="OPEN",
                reason=event.details.get("reason", ""),
            )
        elif event_type == AuditObserverEventType.CIRCUIT_CLOSED:
            self._async_logger.log_cb_event(
                service=event.details.get("service", "audit_primary"),
                state="CLOSED",
            )
        elif event_type == AuditObserverEventType.FALLBACK_ACTIVATED:
            self._async_logger.log_fallback_activated(
                fallback_type=event.details.get("fallback_type", "file"),
                reason=event.details.get("reason", "primary_failed"),
            )
        elif event_type == AuditObserverEventType.SYSLOG_ACTIVATED:
            self._async_logger.log_emergency_event(
                level="CRITICAL",
                action="trigger",
                reason="all_backends_failed",
            )
        elif event_type == AuditObserverEventType.PRIMARY_RECOVERED:
            self._async_logger.log_recovery_event(
                service=event.details.get("service", "audit_primary"),
                recovery_time_ms=event.details.get("recovery_time_ms", 0),
                success=True,
            )
        elif event_type == AuditObserverEventType.DEGRADED_MODE_ENTERED:
            self._async_logger.log_emergency_event(
                level="WARNING",
                action="trigger",
                reason="degraded_mode",
            )
        elif event_type == AuditObserverEventType.RECORD_SUCCESS:
            self._async_logger.log_audit_event(
                action=event.details.get("action", "unknown"),
                success=True,
                audit_id=event.details.get("audit_id", ""),
            )
        elif event_type == AuditObserverEventType.RECORD_FAILED:
            self._async_logger.log_audit_event(
                action=event.details.get("action", "unknown"),
                success=False,
                audit_id=event.details.get("audit_id", ""),
                error=event.details.get("error", ""),
            )


# =============================================================================
# Integrated Audit Recorder
# =============================================================================


class IntegratedAuditRecorder:
    """
    통합 감사 기록기.

    기존 ResilientContinuousAuditRecorder에 다음 기능 추가:
    1. CircuitBreaker 상태 변경 이벤트 외부 전파
    2. SyslogFallback 자동 연결 강화
    3. AsyncLogger 연동 지원 (Observer 패턴)

    Usage:
        recorder = IntegratedAuditRecorder(adapter)
        async_adapter = AsyncLoggerAdapter(flush_callback=send_to_server)
        recorder.attach_async_logger(async_adapter)

        # 이제 record() 호출 시 양쪽에 자동 전파
        recorder.record(entry)
    """

    def __init__(
        self,
        resilient_recorder: ResilientContinuousAuditRecorder,
        enable_auto_async_logging: bool = True,
    ):
        """
        Initialize IntegratedAuditRecorder.

        Args:
            resilient_recorder: 기존 ResilientContinuousAuditRecorder 인스턴스
            enable_auto_async_logging: AsyncLogger 자동 로깅 활성화
        """
        self._recorder = resilient_recorder
        self._enable_auto_async_logging = enable_auto_async_logging

        # Observer list
        self._observers: list[AuditEventObserver] = []
        self._observers_lock = threading.Lock()

        # AsyncLogger adapter (optional)
        self._async_logger: AsyncLoggerAdapter | None = None

        # Circuit state tracking
        self._last_circuit_state = None

    def attach_observer(self, observer: AuditEventObserver) -> None:
        """Observer 등록."""
        with self._observers_lock:
            self._observers.append(observer)
            logger.debug(
                "integrated_recorder.observer_attached",
                adapter_type=type(observer).__name__,
            )

    def detach_observer(self, observer: AuditEventObserver) -> None:
        """Observer 해제."""
        with self._observers_lock:
            if observer in self._observers:
                self._observers.remove(observer)
                logger.debug(
                    "integrated_recorder.observer_detached",
                    adapter_type=type(observer).__name__,
                )

    def attach_async_logger(self, async_logger: AsyncLoggerAdapter) -> None:
        """
        AsyncLoggerAdapter 연결.

        연결 후 모든 감사 이벤트가 AsyncLogger로도 전파됨.
        """
        self._async_logger = async_logger

        # AsyncLogger를 Observer로 등록
        observer = AsyncLoggerObserver(async_logger)
        self.attach_observer(observer)

        # AsyncLogger 시작 (아직 안 됐으면)
        async_logger.start()

        logger.info("integrated_recorder.asyncloggeradapter_attached")

    def _notify_observers(self, event: AuditEventData) -> None:
        """모든 Observer에 이벤트 전파."""
        with self._observers_lock:
            for observer in self._observers:
                try:
                    observer.on_event(event)
                except Exception as e:
                    logger.exception(
                        "integrated_recorder.observer_error",
                        error=e,
                    )

    def _check_circuit_state_change(self) -> None:
        """Circuit Breaker 상태 변경 감지 및 전파."""
        current_state = self._recorder._circuit_breaker.state

        if self._last_circuit_state != current_state:
            if current_state.value == "open":
                self._notify_observers(
                    AuditEventData(
                        event_type=AuditObserverEventType.CIRCUIT_OPENED,
                        details={"service": "audit_primary"},
                    )
                )
            elif current_state.value == "closed" and self._last_circuit_state:
                if self._last_circuit_state.value == "open":
                    self._notify_observers(
                        AuditEventData(
                            event_type=AuditObserverEventType.PRIMARY_RECOVERED,
                            details={"service": "audit_primary"},
                        )
                    )
                self._notify_observers(
                    AuditEventData(
                        event_type=AuditObserverEventType.CIRCUIT_CLOSED,
                        details={"service": "audit_primary"},
                    )
                )
            elif current_state.value == "half_open":
                self._notify_observers(
                    AuditEventData(
                        event_type=AuditObserverEventType.CIRCUIT_HALF_OPEN,
                        details={"service": "audit_primary"},
                    )
                )

            self._last_circuit_state = current_state

    def record_with_events(self, entry: AuditEntry) -> str:
        """
        기록 + 이벤트 전파.

        원본 record 메서드를 래핑하여 이벤트도 함께 전파.
        """
        try:
            # 기존 record 호출
            audit_id = self._recorder._record_with_integrity(entry)

            # 성공 이벤트 전파
            if self._enable_auto_async_logging:
                self._notify_observers(
                    AuditEventData(
                        event_type=AuditObserverEventType.RECORD_SUCCESS,
                        details={
                            "action": entry.action,
                            "audit_id": audit_id,
                        },
                    )
                )

            # Circuit 상태 변경 체크
            self._check_circuit_state_change()

            return audit_id

        except Exception as e:
            # 실패 이벤트 전파
            if self._enable_auto_async_logging:
                self._notify_observers(
                    AuditEventData(
                        event_type=AuditObserverEventType.RECORD_FAILED,
                        details={
                            "action": entry.action,
                            "error": str(e),
                        },
                    )
                )

            # Circuit 상태 변경 체크
            self._check_circuit_state_change()

            raise

    def get_health_status(self) -> dict[str, Any]:
        """통합 헬스 상태 반환."""
        health = self._recorder.get_health_status()

        # AsyncLogger 상태 추가
        if self._async_logger:
            health["async_logger"] = self._async_logger.get_stats()

        # Observer 수 추가
        with self._observers_lock:
            health["observers_count"] = len(self._observers)

        return health

    # Proxy methods
    def start(self) -> None:
        """시작."""
        self._recorder.start()
        if self._async_logger:
            self._async_logger.start()

    def stop(self, timeout: float | None = None) -> None:
        """중지."""
        self._recorder.stop(timeout)
        if self._async_logger:
            self._async_logger.stop(timeout)


# =============================================================================
# Convenience Functions
# =============================================================================


def configure_integration(
    resilient_recorder: ResilientContinuousAuditRecorder,
    flush_callback: Callable[[list[dict[str, Any]]], None] | None = None,
    async_logger_config: AsyncLoggerConfig | None = None,
) -> IntegratedAuditRecorder:
    """
    통합 설정 헬퍼 함수.

    Args:
        resilient_recorder: 기존 ResilientContinuousAuditRecorder
        flush_callback: AsyncLogger 배치 전송 콜백
        async_logger_config: AsyncLogger 설정

    Returns:
        설정된 IntegratedAuditRecorder
    """
    integrated = IntegratedAuditRecorder(resilient_recorder)

    if flush_callback:
        async_logger = AsyncLoggerAdapter(
            flush_callback=flush_callback,
            config=async_logger_config,
        )
        integrated.attach_async_logger(async_logger)

    return integrated


def create_command_center_callback(
    endpoint: str,
    timeout_seconds: float = 5.0,
) -> Callable[[list[dict[str, Any]]], None]:
    """
    Command Center 전송 콜백 생성.

    Args:
        endpoint: Command Center API 엔드포인트
        timeout_seconds: 요청 타임아웃

    Returns:
        배치 전송 콜백 함수
    """
    import urllib.error
    import urllib.request

    from baldur.utils.http import safe_urlopen
    from baldur.utils.serialization import fast_dumps

    def send_to_command_center(events: list[dict[str, Any]]) -> None:
        """이벤트를 Command Center로 전송."""
        try:
            data = fast_dumps(events)
            request = urllib.request.Request(
                endpoint,
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with safe_urlopen(request, timeout=timeout_seconds) as response:
                if response.status != 200:
                    logger.warning(
                        "command.center_returned",
                        response_status=response.status,
                    )
        except urllib.error.URLError as e:
            logger.exception(
                "integrated_recorder.command_center_send_failed",
                error=e,
            )
        except Exception as e:
            logger.exception(
                "command.center_callback_error",
                error=e,
            )

    return send_to_command_center


# =============================================================================
# Export for __init__.py
# =============================================================================

__all__ = [
    "EventSeverity",
    "AsyncLoggerConfig",
    "AsyncLoggerAdapter",
    "AuditObserverEventType",
    "AuditEventData",
    "AuditEventObserver",
    "AsyncLoggerObserver",
    "IntegratedAuditRecorder",
    "configure_integration",
    "create_command_center_callback",
]
