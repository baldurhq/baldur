"""
Audit Watchdog - Dead Man's Switch Pattern.

감사 시스템 생존 확인:
- 주기적으로 heartbeat 전송
- 외부 모니터링 시스템이 heartbeat 감시
- heartbeat 누락 시 알림

Usage:
    from baldur.audit.audit_watchdog import (
        AuditWatchdog,
        AuditWatchdogConfig,
        HeartbeatTarget,
    )

    # 기본 설정으로 시작
    watchdog = AuditWatchdog()
    watchdog.start()

    # 커스텀 설정
    config = AuditWatchdogConfig(
        heartbeat_interval_seconds=30.0,
        missed_threshold=3,
        targets=[
            HeartbeatTarget(
                name="deadmansswitch",
                url="https://deadmansswitch.io/ping/xxxx",
            ),
        ],
    )
    watchdog = AuditWatchdog(config=config)
    watchdog.start()

    # 수동 heartbeat
    watchdog.pet()

    # 종료
    watchdog.stop()

최소 의존성: urllib만 사용 (requests 불필요)
"""

from __future__ import annotations

import json
import os
import threading
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, Any

import structlog

from baldur.audit.self_audit import SelfAuditEvent, self_audit
from baldur.utils.http import safe_urlopen
from baldur.utils.time import utc_now

if TYPE_CHECKING:
    from baldur.settings.audit_watchdog import AuditWatchdogSettings

logger = structlog.get_logger()

_WORKER_NAME = "AuditWatchdog"


class AuditWatchdogStatus(str, Enum):
    """Watchdog 상태."""

    STOPPED = "stopped"
    RUNNING = "running"
    DEGRADED = "degraded"  # heartbeat 전송 실패


@dataclass
class HeartbeatTarget:
    """Heartbeat 전송 대상."""

    name: str
    url: str
    method: str = "GET"  # GET or POST
    headers: dict[str, str] = field(default_factory=dict)
    timeout_seconds: float = 5.0
    enabled: bool = True


@dataclass
class AuditWatchdogConfig:
    """Watchdog 설정."""

    # Heartbeat 주기 (초)
    heartbeat_interval_seconds: float = 30.0

    # 연속 실패 허용 횟수
    missed_threshold: int = 3

    # Heartbeat 전송 대상 목록
    targets: list[HeartbeatTarget] = field(default_factory=list)

    # 로컬 파일 heartbeat (외부 서비스 없이도 작동)
    local_heartbeat_file: str | None = None

    # 콜백 함수들
    on_heartbeat_success: Callable[[], None] | None = None
    on_heartbeat_failure: Callable[[str, Exception], None] | None = None
    on_threshold_exceeded: Callable[[int], None] | None = None

    @classmethod
    def from_settings(
        cls,
        settings: AuditWatchdogSettings | None = None,
        **overrides,
    ) -> AuditWatchdogConfig:
        """
        Settings에서 AuditWatchdogConfig 인스턴스 생성.

        Args:
            settings: AuditWatchdogSettings 인스턴스 (없으면 싱글톤 사용)
            **overrides: 개별 필드 오버라이드

        Returns:
            AuditWatchdogConfig: Settings 기반 인스턴스
        """
        from baldur.settings.audit_watchdog import get_audit_watchdog_settings

        s = settings or get_audit_watchdog_settings()

        # Settings에서 heartbeat_url이 있으면 target 생성
        targets = overrides.get("targets", [])
        if not targets and s.heartbeat_url:
            targets.append(
                HeartbeatTarget(
                    name="env_heartbeat",
                    url=s.heartbeat_url,
                    timeout_seconds=s.timeout_seconds,
                )
            )

        return cls(
            heartbeat_interval_seconds=overrides.get(
                "heartbeat_interval_seconds", s.heartbeat_interval_seconds
            ),
            missed_threshold=overrides.get("missed_threshold", s.missed_threshold),
            targets=targets,
            local_heartbeat_file=overrides.get(
                "local_heartbeat_file", s.local_heartbeat_file
            ),
            on_heartbeat_success=overrides.get("on_heartbeat_success"),
            on_heartbeat_failure=overrides.get("on_heartbeat_failure"),
            on_threshold_exceeded=overrides.get("on_threshold_exceeded"),
        )


@dataclass
class WatchdogStats:
    """Watchdog 통계."""

    total_heartbeats: int = 0
    successful_heartbeats: int = 0
    failed_heartbeats: int = 0
    consecutive_failures: int = 0
    last_heartbeat_time: datetime | None = None
    last_failure_time: datetime | None = None
    last_failure_reason: str | None = None
    uptime_seconds: float = 0.0


class AuditWatchdog:
    """
    Dead Man's Switch 패턴 Watchdog.

    특징:
    - 주기적 heartbeat 전송
    - 외부/로컬 heartbeat 지원
    - 연속 실패 감지 및 알림
    - 수동 heartbeat (pet) 지원
    - Thread-safe
    """

    def __init__(
        self,
        config: AuditWatchdogConfig | None = None,
        on_alive: Callable[[], None] | None = None,
        on_dead: Callable[[int], None] | None = None,
    ):
        """
        Initialize AuditWatchdog.

        Args:
            config: Watchdog 설정
            on_alive: 정상 heartbeat 콜백 (deprecated, use config.on_heartbeat_success)
            on_dead: 임계값 초과 콜백 (deprecated, use config.on_threshold_exceeded)
        """
        self._config = config or AuditWatchdogConfig.from_settings()
        self._state = AuditWatchdogStatus.STOPPED
        self._stats = WatchdogStats()
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._lock = threading.RLock()
        self._start_time: datetime | None = None
        self._handle: Any | None = None  # DaemonWorkerHandle (impl 489 D9)

        # 레거시 콜백 지원
        if on_alive and not self._config.on_heartbeat_success:
            self._config.on_heartbeat_success = on_alive
        if on_dead and not self._config.on_threshold_exceeded:
            self._config.on_threshold_exceeded = on_dead

    @property
    def state(self) -> AuditWatchdogStatus:
        """현재 상태 조회."""
        return self._state

    @property
    def is_running(self) -> bool:
        """실행 중 여부."""
        return self._state in (
            AuditWatchdogStatus.RUNNING,
            AuditWatchdogStatus.DEGRADED,
        )

    def start(self) -> None:
        """Watchdog 시작."""
        from baldur.meta.daemon_worker import DaemonWorkerHandle
        from baldur.metrics.recorders.daemon_worker import register_daemon_worker

        with self._lock:
            if self._state != AuditWatchdogStatus.STOPPED:
                logger.warning("audit_watchdog.already_running")
                return

            self._state = AuditWatchdogStatus.RUNNING
            self._start_time = utc_now()
            self._stop_event.clear()

            self._spawn_thread()
            assert self._thread is not None  # _spawn_thread() invariant
            self._handle = DaemonWorkerHandle(
                thread=self._thread,
                tick_interval_seconds=self._config.heartbeat_interval_seconds,
                restart_callback=self._spawn_thread,
            )
            register_daemon_worker(_WORKER_NAME, self._handle)

            self_audit().log(
                SelfAuditEvent.STARTUP,
                "Audit Watchdog started",
                details={
                    "interval_seconds": self._config.heartbeat_interval_seconds,
                    "targets": len(self._config.targets),
                },
            )
            logger.info(
                "audit.watchdog_started",
                heartbeat_interval_seconds=self._config.heartbeat_interval_seconds,
            )

    def _spawn_thread(self) -> None:
        """Construct + start a fresh heartbeat thread (impl 489 D9 respawn helper)."""
        self._thread = threading.Thread(
            target=self._heartbeat_loop_with_crash_capture,
            daemon=True,
            name=_WORKER_NAME,
        )
        self._thread.start()
        if self._handle is not None:
            self._handle.thread = self._thread

    def _heartbeat_loop_with_crash_capture(self) -> None:
        try:
            self._heartbeat_loop()
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as e:
            if self._handle is not None:
                self._handle.record_crash(e)
            raise

    def stop(self, timeout: float | None = None) -> None:
        """Watchdog 중지."""
        from baldur.metrics.recorders.daemon_worker import unregister_daemon_worker

        if timeout is None:
            from baldur.settings.thread_management import (
                get_thread_management_settings,
            )

            timeout = get_thread_management_settings().join_timeout
        with self._lock:
            if self._state == AuditWatchdogStatus.STOPPED:
                return

            if self._handle is not None:
                self._handle.is_stopping = True

            self._stop_event.set()
            self._state = AuditWatchdogStatus.STOPPED

        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)

        unregister_daemon_worker(_WORKER_NAME)
        if self._thread is not None and self._thread.is_alive():
            logger.critical(
                "daemon_worker.stop_join_timeout",
                worker_name=_WORKER_NAME,
                join_timeout_seconds=timeout,
            )

        self_audit().log(
            SelfAuditEvent.SHUTDOWN,
            "Audit Watchdog stopped",
            details=self.get_stats().__dict__,
        )
        logger.info("audit_watchdog.stopped")

    def pet(self) -> None:
        """
        수동 heartbeat (정상 작동 확인).

        주기적 heartbeat 외에 수동으로 heartbeat를 보낼 때 사용.
        연속 실패 카운터를 리셋함.
        """
        with self._lock:
            self._stats.consecutive_failures = 0
            self._stats.last_heartbeat_time = utc_now()
            self._send_heartbeat()

    def get_stats(self) -> WatchdogStats:
        """통계 조회."""
        with self._lock:
            if self._start_time:
                self._stats.uptime_seconds = (
                    utc_now() - self._start_time
                ).total_seconds()
            return WatchdogStats(
                total_heartbeats=self._stats.total_heartbeats,
                successful_heartbeats=self._stats.successful_heartbeats,
                failed_heartbeats=self._stats.failed_heartbeats,
                consecutive_failures=self._stats.consecutive_failures,
                last_heartbeat_time=self._stats.last_heartbeat_time,
                last_failure_time=self._stats.last_failure_time,
                last_failure_reason=self._stats.last_failure_reason,
                uptime_seconds=self._stats.uptime_seconds,
            )

    def _heartbeat_loop(self) -> None:
        """Heartbeat 루프 (백그라운드 스레드)."""
        import time as _time

        while not self._stop_event.is_set():
            iter_start = _time.monotonic()
            try:
                self._send_heartbeat()
            except Exception as e:
                logger.exception(
                    "heartbeat.loop_error",
                    error=e,
                )

            if self._handle is not None:
                self._handle.observe_iteration(_time.monotonic() - iter_start)
                self._handle.heartbeat()

            # interval 동안 대기 (stop_event로 중단 가능)
            self._stop_event.wait(timeout=self._config.heartbeat_interval_seconds)

    def _send_heartbeat(self) -> None:  # noqa: C901, PLR0912
        """Heartbeat 전송."""
        with self._lock:
            self._stats.total_heartbeats += 1
            success = True
            failure_reason = None

            # 1. 외부 타겟에 heartbeat 전송
            for target in self._config.targets:
                if not target.enabled:
                    continue

                try:
                    self._send_to_target(target)
                except Exception as e:
                    success = False
                    failure_reason = f"{target.name}: {str(e)}"
                    logger.warning(
                        "heartbeat.failed",
                        target_name=target.name,
                        error=e,
                    )

                    if self._config.on_heartbeat_failure:
                        try:
                            self._config.on_heartbeat_failure(target.name, e)
                        except Exception:
                            pass

            # 2. 로컬 파일 heartbeat
            if self._config.local_heartbeat_file:
                try:
                    self._write_local_heartbeat()
                except Exception as e:
                    success = False
                    failure_reason = f"local_file: {str(e)}"
                    logger.warning(
                        "local.heartbeat_failed",
                        error=e,
                    )

            # 3. 결과 처리
            now = utc_now()

            if success:
                self._stats.successful_heartbeats += 1
                self._stats.consecutive_failures = 0
                self._stats.last_heartbeat_time = now
                self._state = AuditWatchdogStatus.RUNNING

                if self._config.on_heartbeat_success:
                    try:
                        self._config.on_heartbeat_success()
                    except Exception:
                        pass
            else:
                self._stats.failed_heartbeats += 1
                self._stats.consecutive_failures += 1
                self._stats.last_failure_time = now
                self._stats.last_failure_reason = failure_reason
                self._state = AuditWatchdogStatus.DEGRADED

                self_audit().log(
                    SelfAuditEvent.HEARTBEAT_MISSED,
                    f"Heartbeat failed (consecutive: {self._stats.consecutive_failures})",
                    details={"reason": failure_reason},
                )

                # 임계값 초과 체크
                if self._stats.consecutive_failures >= self._config.missed_threshold:
                    self_audit().log(
                        SelfAuditEvent.WATCHDOG_TIMEOUT,
                        f"Heartbeat threshold exceeded: {self._stats.consecutive_failures}",
                    )

                    if self._config.on_threshold_exceeded:
                        try:
                            self._config.on_threshold_exceeded(
                                self._stats.consecutive_failures
                            )
                        except Exception:
                            pass

    def _send_to_target(self, target: HeartbeatTarget) -> None:
        """외부 타겟에 heartbeat 전송."""
        headers = {
            "User-Agent": "AuditWatchdog/1.0",
            "Content-Type": "application/json",
            **target.headers,
        }

        if target.method.upper() == "POST":
            data = json.dumps(
                {
                    "timestamp": utc_now().isoformat(),
                    "source": "audit_watchdog",
                }
            ).encode("utf-8")
            request = urllib.request.Request(
                target.url,
                data=data,
                headers=headers,
                method="POST",
            )
        else:
            request = urllib.request.Request(
                target.url,
                headers=headers,
                method="GET",
            )

        with safe_urlopen(request, timeout=target.timeout_seconds) as response:
            _ = response.read()  # 응답 소비

    def _write_local_heartbeat(self) -> None:
        """로컬 파일에 heartbeat 기록."""
        if not self._config.local_heartbeat_file:
            return

        heartbeat_data = {
            "timestamp": utc_now().isoformat(),
            "pid": os.getpid(),
            "stats": {
                "total": self._stats.total_heartbeats,
                "successful": self._stats.successful_heartbeats,
                "failed": self._stats.failed_heartbeats,
            },
        }

        with open(self._config.local_heartbeat_file, "w") as f:
            json.dump(heartbeat_data, f, indent=2)


class WatchdogChecker:
    """
    Watchdog 상태 검사기.

    로컬 heartbeat 파일을 읽어서 Watchdog 상태 확인.
    외부 모니터링 스크립트나 헬스체크에서 사용.
    """

    def __init__(
        self,
        heartbeat_file: str,
        max_age_seconds: float = 60.0,
    ):
        """
        Initialize WatchdogChecker.

        Args:
            heartbeat_file: heartbeat 파일 경로
            max_age_seconds: heartbeat 최대 허용 경과 시간
        """
        self._heartbeat_file = heartbeat_file
        self._max_age_seconds = max_age_seconds

    def is_alive(self) -> bool:
        """Watchdog 생존 여부 확인."""
        try:
            heartbeat = self.read_heartbeat()
            if not heartbeat:
                return False

            timestamp_str = heartbeat.get("timestamp")
            if not timestamp_str:
                return False

            timestamp = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
            age = (utc_now() - timestamp).total_seconds()

            return age <= self._max_age_seconds
        except Exception:
            return False

    def read_heartbeat(self) -> dict[str, Any] | None:
        """Heartbeat 파일 읽기."""
        try:
            with open(self._heartbeat_file) as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return None

    def get_age_seconds(self) -> float | None:
        """마지막 heartbeat 이후 경과 시간."""
        try:
            heartbeat = self.read_heartbeat()
            if not heartbeat:
                return None

            timestamp_str = heartbeat.get("timestamp")
            if not timestamp_str:
                return None

            timestamp = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
            return (utc_now() - timestamp).total_seconds()
        except Exception:
            return None


# 싱글톤 인스턴스 관리
_watchdog_instance: AuditWatchdog | None = None
_watchdog_lock = threading.Lock()


def get_watchdog() -> AuditWatchdog:
    """싱글톤 Watchdog 인스턴스 조회."""
    global _watchdog_instance
    if _watchdog_instance is None:
        with _watchdog_lock:
            if _watchdog_instance is None:
                _watchdog_instance = AuditWatchdog()
    return _watchdog_instance


def start_watchdog(config: AuditWatchdogConfig | None = None) -> AuditWatchdog:
    """Watchdog 시작 (편의 함수)."""
    global _watchdog_instance
    with _watchdog_lock:
        if _watchdog_instance is not None:
            _watchdog_instance.stop()
        _watchdog_instance = AuditWatchdog(config=config)
        _watchdog_instance.start()
    return _watchdog_instance


def stop_watchdog() -> None:
    """Watchdog 중지 (편의 함수)."""
    global _watchdog_instance
    with _watchdog_lock:
        if _watchdog_instance is not None:
            _watchdog_instance.stop()
            _watchdog_instance = None
