"""
Partition Reconciliation Service.

네트워크 고립(Partition) 복구 시 상태 조정 서비스.

주요 기능:
- check_partition_status(): 현재 리전의 네트워크 고립 상태 확인
- reconcile_after_recovery(): 네트워크 복구 후 상태 조정
- start_heartbeat_loop(): 백그라운드 heartbeat 모니터링 시작
- stop_heartbeat_loop(): heartbeat 모니터링 중지

설계 원칙:
- 고립된 리전은 자체 상태 유지 (Safety-First)
- TTL 기반 자동 만료로 영구 stale 상태 방지
- 복구 시 강제 동기화 없이 운영자 알림

Code reference:
    error_budget/reconciliation/service.py (ReconciliationService 패턴)
    isolation/regional_gate.py#L133-141 (TTL 기반 만료)
    core/tiered_redis.py (TieredRedisProvider)

Reference:
    docs/baldur/middleware_system/73_NAMESPACE_AWARE_EMERGENCY.md
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

import structlog

from baldur.core.serializable import SerializableMixin
from baldur.utils.time import utc_now

if TYPE_CHECKING:
    from baldur.meta.daemon_worker import DaemonWorkerHandle

logger = structlog.get_logger()


# =============================================================================
# Constants
# =============================================================================

HEARTBEAT_INTERVAL_SECONDS = 10
"""Heartbeat 전송 간격 (초)."""

PARTITION_DETECTION_THRESHOLD_SECONDS = 30
"""네트워크 고립 감지 임계값 (초). 이 시간 동안 heartbeat 실패 시 고립으로 판단."""

MAX_RECONCILIATION_ACTIONS = 100
"""조정 액션 히스토리 최대 크기."""


@dataclass
class PartitionStatus(SerializableMixin):
    """
    네트워크 고립 상태.

    현재 리전의 Global Redis 연결 상태를 나타냅니다.
    """

    is_partitioned: bool = False
    """고립 여부."""

    last_heartbeat_at: str | None = None
    """마지막 heartbeat 성공 시각 (ISO format)."""

    partition_duration_seconds: float = 0.0
    """고립 지속 시간 (초)."""

    error_message: str | None = None
    """연결 실패 시 에러 메시지."""

    global_redis_url: str | None = None
    """Global Redis URL (마스킹됨)."""

    checked_at: str = field(default_factory=lambda: utc_now().isoformat())
    """확인 시각."""


@dataclass
class ReconciliationAction(SerializableMixin):
    """
    조정 액션.

    네트워크 복구 후 수행된 조정 액션.
    """

    action_type: str = ""
    """액션 유형 (NOTIFICATION, STATE_SYNC, MANUAL_REVIEW)."""

    message: str = ""
    """액션 상세 메시지."""

    namespace: str = ""
    """대상 네임스페이스."""

    executed_at: str = field(default_factory=lambda: utc_now().isoformat())
    """실행 시각."""

    success: bool = True
    """실행 성공 여부."""


@dataclass
class ReconciliationResult(SerializableMixin):
    """
    조정 결과.

    reconcile_after_recovery() 호출 결과.
    """

    reconciled: bool = False
    """조정 수행 여부."""

    reason: str | None = None
    """조정 실패 또는 스킵 사유."""

    actions: list[ReconciliationAction] = field(default_factory=list)
    """수행된 액션 목록."""

    global_state_mode: str = "UNKNOWN"
    """Global Emergency 모드."""

    regional_state_mode: str = "UNKNOWN"
    """Regional Emergency 모드."""

    executed_at: str = field(default_factory=lambda: utc_now().isoformat())
    """실행 시각."""


class PartitionReconciliationService:
    """
    네트워크 고립 복구 시 상태 조정 서비스.

    Global Redis와의 연결 상태를 모니터링하고,
    네트워크 복구 후 상태 불일치를 조정합니다.

    설계 원칙:
    - Safety-First: 고립된 리전은 자체 보호 모드 유지
    - 강제 동기화 없음: 복구 시 운영자 알림으로 대체
    - TTL 기반 만료: 영구 stale 상태 방지

    Usage:
        service = PartitionReconciliationService()

        # 고립 상태 확인
        status = service.check_partition_status()
        if status.is_partitioned:
            print(f"Partitioned for {status.partition_duration_seconds}s")

        # 복구 후 조정
        result = service.reconcile_after_recovery()
        for action in result.actions:
            print(f"Action: {action.action_type} - {action.message}")
    """

    def __init__(
        self,
        tracker: Any | None = None,
        tiered_redis: Any | None = None,
        heartbeat_interval: int = HEARTBEAT_INTERVAL_SECONDS,
        partition_threshold: int = PARTITION_DETECTION_THRESHOLD_SECONDS,
    ):
        """
        PartitionReconciliationService 초기화.

        Args:
            tracker: NamespacedEmergencyTracker 인스턴스
            tiered_redis: TieredRedisProvider 인스턴스
            heartbeat_interval: Heartbeat 간격 (초)
            partition_threshold: 고립 감지 임계값 (초)
        """
        self._tracker = tracker
        self._tiered_redis = tiered_redis
        self._heartbeat_interval = heartbeat_interval
        self._partition_threshold = partition_threshold
        self._lock = threading.Lock()

        # 상태
        self._last_global_heartbeat: datetime | None = None
        self._is_partitioned = False
        self._partition_start: datetime | None = None

        # 액션 히스토리
        self._action_history: list[ReconciliationAction] = []

        # Heartbeat 스레드
        self._heartbeat_thread: threading.Thread | None = None
        self._heartbeat_running = False
        self._handle: DaemonWorkerHandle | None = None  # impl 489 D9

    # =========================================================================
    # Dependency Injection
    # =========================================================================

    def _get_tracker(self) -> Any:
        """NamespacedEmergencyTracker 인스턴스 획득."""
        if self._tracker is None:
            from baldur.services.regional_emergency.tracker import (
                get_namespaced_emergency_tracker,
            )

            self._tracker = get_namespaced_emergency_tracker()
        return self._tracker

    def _get_tiered_redis(self) -> Any:
        """TieredRedisProvider 인스턴스 획득."""
        if self._tiered_redis is None:
            try:
                from baldur.core.tiered_redis import TieredRedisProvider

                self._tiered_redis = TieredRedisProvider()
            except ImportError:
                logger.warning("partition_reconciliation.tieredredisprovider_available")
        return self._tiered_redis

    def _get_current_namespace(self) -> str:
        """현재 리전의 네임스페이스 획득."""
        try:
            from baldur.core.cluster_identity import get_cluster_identity

            identity = get_cluster_identity()
            return identity.region or "global"
        except Exception:
            return "global"

    # =========================================================================
    # Partition Detection
    # =========================================================================

    def check_partition_status(self) -> PartitionStatus:
        """
        현재 리전의 네트워크 고립 상태 확인.

        Global Redis에 ping을 시도하여 연결 상태를 확인합니다.

        Returns:
            PartitionStatus 인스턴스
        """
        now = utc_now()

        try:
            # Global Redis ping
            success = self._ping_global_redis()

            if success:
                with self._lock:
                    self._last_global_heartbeat = now
                    self._is_partitioned = False
                    self._partition_start = None

                return PartitionStatus(
                    is_partitioned=False,
                    last_heartbeat_at=now.isoformat(),
                    partition_duration_seconds=0.0,
                    global_redis_url=self._get_masked_redis_url(),
                )
            raise ConnectionError("Ping returned False")

        except Exception as e:
            # 연결 실패
            with self._lock:
                if self._last_global_heartbeat:
                    duration = (now - self._last_global_heartbeat).total_seconds()
                    self._is_partitioned = duration > self._partition_threshold

                    if self._is_partitioned and self._partition_start is None:
                        self._partition_start = now
                else:
                    self._is_partitioned = True
                    self._partition_start = self._partition_start or now
                    duration = float("inf")

                return PartitionStatus(
                    is_partitioned=self._is_partitioned,
                    last_heartbeat_at=(
                        self._last_global_heartbeat.isoformat()
                        if self._last_global_heartbeat
                        else None
                    ),
                    partition_duration_seconds=duration,
                    error_message=str(e),
                    global_redis_url=self._get_masked_redis_url(),
                )

    def _ping_global_redis(self) -> bool:
        """
        Global Redis ping.

        Returns:
            True if ping successful, False otherwise
        """
        tiered_redis = self._get_tiered_redis()

        if tiered_redis is None:
            # TieredRedisProvider 없으면 단순 Redis ping 시도
            try:
                from baldur.core.state_backend import get_state_backend

                backend = get_state_backend()
                # StateBackend에 ping 메서드가 있으면 사용
                if hasattr(backend, "ping"):
                    return backend.ping()
                # 없으면 간단한 get 시도
                backend.get("__ping_test__")
                return True
            except Exception as e:
                logger.debug(
                    "partition_reconciliation.fallback_ping_failed",
                    error=e,
                )
                return False

        try:
            from baldur.core.tiered_redis import RedisScope

            client = tiered_redis.get_redis(RedisScope.GLOBAL)
            return client.ping()
        except Exception as e:
            logger.debug(
                "partition_reconciliation.global_redis_ping_failed",
                error=e,
            )
            return False

    def _get_masked_redis_url(self) -> str:
        """Redis URL 마스킹 (보안)."""
        import os

        url = os.environ.get("REDIS_GLOBAL_URL") or os.environ.get("REDIS_URL", "")
        if "://" in url:
            # redis://user:password@host:port/db -> redis://***@host:port/db
            parts = url.split("@")
            if len(parts) > 1:
                return f"***@{parts[-1]}"
        return url[:20] + "..." if len(url) > 20 else url

    # =========================================================================
    # Reconciliation
    # =========================================================================

    def reconcile_after_recovery(self) -> ReconciliationResult:
        """
        네트워크 복구 후 상태 조정.

        Global과 Regional 상태를 비교하고 필요시 운영자에게 알립니다.
        강제 동기화는 수행하지 않습니다 (Safety-First).

        Returns:
            ReconciliationResult 인스턴스
        """
        # 아직 고립 상태면 스킵
        if self._is_partitioned:
            return ReconciliationResult(
                reconciled=False,
                reason="Still partitioned - cannot reconcile",
            )

        tracker = self._get_tracker()
        current_ns = self._get_current_namespace()
        actions: list[ReconciliationAction] = []

        try:
            # 상태 조회
            global_state = tracker.get_state(namespace="global")
            regional_state = tracker.get_state(namespace=current_ns)

            global_mode = global_state.governance_mode
            regional_mode = regional_state.governance_mode

            # 상태 불일치 확인 및 액션 생성
            if not global_state.is_active and regional_state.is_active:
                # Global은 NORMAL인데 Regional은 아직 STRICT
                action = ReconciliationAction(
                    action_type="MANUAL_REVIEW",
                    message=(
                        f"Region {current_ns} is still in {regional_mode} mode "
                        f"while Global is {global_mode}. "
                        "Manual review recommended."
                    ),
                    namespace=current_ns,
                )
                actions.append(action)
                self._log_action(action)

            elif global_state.is_active and not regional_state.is_active:
                # Global은 STRICT인데 Regional은 NORMAL (복구 중 누락?)
                action = ReconciliationAction(
                    action_type="NOTIFICATION",
                    message=(
                        f"Global is in {global_mode} mode but "
                        f"region {current_ns} is {regional_mode}. "
                        "State will be synchronized via normal propagation."
                    ),
                    namespace=current_ns,
                )
                actions.append(action)
                self._log_action(action)

            elif global_state.is_active and regional_state.is_active:
                # 둘 다 활성화 - 레벨 비교
                global_level = getattr(global_state.emergency_level, "value", 0)
                regional_level = getattr(regional_state.emergency_level, "value", 0)

                if global_level != regional_level:
                    action = ReconciliationAction(
                        action_type="NOTIFICATION",
                        message=(
                            f"Emergency level mismatch: "
                            f"Global={global_level}, Regional={regional_level} "
                            f"for namespace {current_ns}."
                        ),
                        namespace=current_ns,
                    )
                    actions.append(action)
                    self._log_action(action)

            # 정상 복구 로그
            if not actions:
                logger.info(
                    "partition_reconciliation.recovery_complete_no_actions",
                    current_ns=current_ns,
                    global_mode=global_mode,
                    regional_mode=regional_mode,
                )

            return ReconciliationResult(
                reconciled=True,
                actions=actions,
                global_state_mode=global_mode,
                regional_state_mode=regional_mode,
            )

        except Exception as e:
            logger.exception(
                "partition_reconciliation.reconciliation_failed",
                error=e,
            )
            return ReconciliationResult(
                reconciled=False,
                reason=f"Reconciliation error: {e}",
            )

    def _log_action(self, action: ReconciliationAction) -> None:
        """액션 히스토리에 기록."""
        with self._lock:
            self._action_history.append(action)
            # 히스토리 크기 제한
            if len(self._action_history) > MAX_RECONCILIATION_ACTIONS:
                self._action_history = self._action_history[
                    -MAX_RECONCILIATION_ACTIONS:
                ]

        logger.warning(
            "partition_reconciliation.action",
            reconciliation_action_type=action.action_type,
            reconciliation_message=action.message,
        )

    # =========================================================================
    # Heartbeat Loop
    # =========================================================================

    def start_heartbeat_loop(self) -> None:
        """
        백그라운드 heartbeat 모니터링 시작.
        """
        from baldur.meta.daemon_worker import DaemonWorkerHandle
        from baldur.metrics.recorders.daemon_worker import register_daemon_worker

        if self._heartbeat_running:
            logger.warning("partition_reconciliation.heartbeat_loop_already_running")
            return

        self._heartbeat_running = True
        self._spawn_heartbeat_thread()
        assert self._heartbeat_thread is not None  # spawn always sets non-None
        self._handle = DaemonWorkerHandle(
            thread=self._heartbeat_thread,
            tick_interval_seconds=float(self._heartbeat_interval),
            restart_callback=self._spawn_heartbeat_thread,
        )
        register_daemon_worker("PartitionHeartbeat", self._handle)

        logger.info(
            "partition_reconciliation.heartbeat_loop_started",
            heartbeat_interval=self._heartbeat_interval,
            partition_threshold=self._partition_threshold,
        )

    def _spawn_heartbeat_thread(self) -> None:
        """Construct + start a fresh heartbeat thread (impl 489 D9)."""
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop_with_crash_capture,
            daemon=True,
            name="PartitionHeartbeat",
        )
        self._heartbeat_thread.start()
        if self._handle is not None:
            self._handle.thread = self._heartbeat_thread

    def _heartbeat_loop_with_crash_capture(self) -> None:
        try:
            self._heartbeat_loop()
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as e:
            if self._handle is not None:
                self._handle.record_crash(e)
            raise

    def stop_heartbeat_loop(self) -> None:
        """Heartbeat 모니터링 중지."""
        from baldur.metrics.recorders.daemon_worker import unregister_daemon_worker

        if self._handle is not None:
            self._handle.is_stopping = True
        self._heartbeat_running = False

        if self._heartbeat_thread and self._heartbeat_thread.is_alive():
            from baldur.settings.thread_management import (
                get_thread_management_settings,
            )

            timeout = get_thread_management_settings().join_timeout
            self._heartbeat_thread.join(timeout=timeout)
            unregister_daemon_worker("PartitionHeartbeat")
            if self._heartbeat_thread.is_alive():
                logger.critical(
                    "daemon_worker.stop_join_timeout",
                    worker_name="PartitionHeartbeat",
                    join_timeout_seconds=timeout,
                )
        else:
            unregister_daemon_worker("PartitionHeartbeat")

        logger.info("partition_reconciliation.heartbeat_loop_stopped")

    def _heartbeat_loop(self) -> None:
        """Heartbeat 루프 (백그라운드 스레드)."""
        was_partitioned = False

        while self._heartbeat_running:
            iter_start = time.monotonic()
            try:
                status = self.check_partition_status()

                # 고립 상태 변화 감지
                if status.is_partitioned and not was_partitioned:
                    logger.warning(
                        "partition_reconciliation.partition_detected",
                        partition_duration_seconds=status.partition_duration_seconds,
                    )
                elif not status.is_partitioned and was_partitioned:
                    logger.info(
                        "partition_reconciliation.partition_recovered_triggering_reconciliation"
                    )
                    self.reconcile_after_recovery()

                was_partitioned = status.is_partitioned

            except Exception as e:
                logger.exception(
                    "partition_reconciliation.heartbeat_error",
                    error=e,
                )

            if self._handle is not None:
                self._handle.observe_iteration(time.monotonic() - iter_start)
                self._handle.heartbeat()

            time.sleep(self._heartbeat_interval)

    # =========================================================================
    # Query Methods
    # =========================================================================

    def get_recent_actions(self, limit: int = 10) -> list[dict[str, Any]]:
        """
        최근 조정 액션 조회.

        Args:
            limit: 반환할 최대 개수

        Returns:
            액션 목록 (최신순)
        """
        with self._lock:
            actions = self._action_history[-limit:]
            return [a.to_dict() for a in reversed(actions)]

    def is_partitioned(self) -> bool:
        """
        현재 고립 상태 여부.

        check_partition_status() 호출 없이 마지막 상태 반환.
        """
        return self._is_partitioned

    def get_partition_duration(self) -> float | None:
        """
        현재 고립 지속 시간 (초).

        Returns:
            고립 지속 시간 (고립 중이 아니면 None)
        """
        if not self._is_partitioned or self._partition_start is None:
            return None

        now = utc_now()
        return (now - self._partition_start).total_seconds()


# =============================================================================
# Singleton
# =============================================================================

_reconciliation_service: PartitionReconciliationService | None = None
_service_lock = threading.Lock()


def get_partition_reconciliation_service() -> PartitionReconciliationService:
    """PartitionReconciliationService 싱글톤 반환."""
    global _reconciliation_service
    if _reconciliation_service is None:
        with _service_lock:
            if _reconciliation_service is None:
                _reconciliation_service = PartitionReconciliationService()
    return _reconciliation_service


def reset_partition_reconciliation_service() -> None:
    """
    싱글톤 초기화 (테스트용).

    테스트 간 격리를 위해 싱글톤 인스턴스를 제거합니다.
    """
    global _reconciliation_service
    with _service_lock:
        if _reconciliation_service is not None:
            _reconciliation_service.stop_heartbeat_loop()
        _reconciliation_service = None
