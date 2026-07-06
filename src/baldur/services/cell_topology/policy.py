"""
Cell 대피 정책 — Tick-Based State Machine + Hysteresis.

LeaderScheduler의 aggregate_all() 루프에서 매 tick마다 호출되는
멱등성 상태 전이 함수입니다.

아키텍처 결정:
- 히스테리시스 — 연속 카운터(CellInfo.metadata) + 상태 전이 시 양방향 리셋
- Tick-Based State Machine — threading.Thread/time.sleep() 제거
- bulkhead.max_concurrent = 0 제거 — Hash Ring 라우팅이 전역 차단 담당
- max_evacuated_ratio — Cascading Failure 방지 하드 리미트
- CellRegistry = SoT — Gate/Blast는 Celery Fire-and-forget 통보

의존성:
- CellRegistry: Cell 상태 관리 (SoT, Control Plane)
- RegionalIsolationGate: 감사 로그/이벤트 발행 (통보, Fire-and-forget)
- BlastRadiusService: 감사 로그 (통보, Fire-and-forget)
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

import structlog

from baldur.services.event_bus.bus.event_types import EventType
from baldur.services.event_bus.emitter import EventEmitterMixin
from baldur.utils.time import utc_now

if TYPE_CHECKING:
    from baldur.services.cell_topology.models import (  # noqa: F401
        CellInfo,
    )
    from baldur.services.cell_topology.models import (
        CellState as _CellState,
    )
    from baldur.services.cell_topology.registry import CellRegistry
    from baldur.settings.cell_topology import CellTopologySettings

logger = structlog.get_logger()


@dataclass
class EvacuationRecord:
    """대피 기록."""

    cell_id: str
    trigger_health_score: float
    started_at: datetime = field(default_factory=lambda: utc_now())
    completed_at: datetime | None = None
    reason: str = ""
    affected_services: list[str] = field(default_factory=list)


class CellEvacuationPolicy(EventEmitterMixin):
    """
    Cell 대피 정책 — Tick-Based State Machine.

    evaluate()는 LeaderScheduler의 aggregate_all() 루프에서
    매 tick(health_check_interval_seconds=10초)마다 호출됩니다.

    각 호출에서 Cell의 현재 상태(CellState)와 metadata를 읽고,
    다음 상태 전이를 결정합니다. 스레드를 생성하지 않습니다.

    Leader handoff safety: last_state_change, last_state_change_time은
    L2(Redis)에 동기화됩니다 (doc 388, Q6). 히스테리시스 카운터
    (evacuation_below_count, recovery_above_count)는 의도적으로
    제외 — 리셋이 보수적 안전 경로입니다.

    토글:
    - CellTopologySettings.evacuation_enabled=True일 때만 동작
    - False이면 evaluate() 즉시 반환

    사용:
        policy = get_cell_evacuation_policy()
        for cell_id, info in registry.get_all_cells().items():
            policy.evaluate(cell_id, info.health_score)
    """

    _event_source = "cell_evacuation_policy"

    def __init__(self, settings: CellTopologySettings | None = None):
        from baldur.settings.cell_topology import get_cell_topology_settings

        self._settings = settings or get_cell_topology_settings()
        self._evacuation_history: deque[EvacuationRecord] = deque(
            maxlen=self._settings.evacuation_history_max_size,
        )

    # =========================================================================
    # evaluate() — 상태 머신 Tick
    # =========================================================================

    def evaluate(self, cell_id: str, health_score: float) -> bool:
        """
        상태 머신 Tick — 매 호출마다 Cell의 다음 전이를 결정.

        멱등성: 같은 상태에서 같은 입력이면 같은 결과.
        리더 교체 안전: CellInfo.metadata에 모든 카운터/시간 영속화.

        Args:
            cell_id: Cell 식별자
            health_score: 현재 건강도 (0.0~1.0)

        Returns:
            상태 전이 발생 여부
        """
        if not self._settings.enabled or not self._settings.evacuation_enabled:
            return False

        from baldur.services.cell_topology import get_cell_registry
        from baldur.services.cell_topology.models import CellState

        registry = get_cell_registry()
        cell = registry.get_cell_info(cell_id)
        if not cell:
            return False

        state = cell.state

        # WARMUP Cell은 대피 평가 대상 아님
        if state == CellState.WARMUP:
            return False

        if state == CellState.ACTIVE:
            return self._tick_active(
                cell_id,
                health_score,
                cell,
                registry,
                CellState,
            )

        if state == CellState.DRAINING:
            return self._tick_draining(
                cell_id,
                cell,
                registry,
                CellState,
            )

        if state == CellState.ISOLATED:
            return self._tick_isolated(
                cell_id,
                health_score,
                cell,
                registry,
                CellState,
            )

        return False

    # =========================================================================
    # State: ACTIVE — 히스테리시스 기반 대피 판단
    # =========================================================================

    def _tick_active(
        self,
        cell_id: str,
        health_score: float,
        cell: CellInfo,
        registry: CellRegistry,
        CellState: type[_CellState],
    ) -> bool:
        """
        ACTIVE 상태 Cell의 대피 필요 여부 평가.

        히스테리시스: 연속 evacuation_consecutive_count(기본 3)회
        임계치 이하일 때만 DRAINING으로 전환.
        임계치를 초과하면 below_count를 즉시 0으로 리셋.
        """
        threshold = self._settings.evacuation_health_threshold

        if health_score <= threshold:
            # 임계치 이하 — 카운터 증가
            below_count = cell.metadata.get("evacuation_below_count", 0) + 1
            cell.metadata["evacuation_below_count"] = below_count

            if below_count < self._settings.evacuation_consecutive_count:
                logger.debug(
                    "cell_evacuation.health_below_threshold",
                    cell_id=cell_id,
                    health_score=health_score,
                    threshold=threshold,
                    below_count=below_count,
                    required_count=self._settings.evacuation_consecutive_count,
                )
                return False

            # === Global Evacuation Limit — Cascading Failure 방지 ===
            if not self._check_global_evacuation_limit(cell_id, registry):
                return False

            # === 연속 카운터 도달 — DRAINING 전환 ===
            reason = (
                f"Health score {health_score:.2f} <= {threshold} "
                f"for {below_count} consecutive ticks"
            )
            logger.warning(
                "cell_evacuation.draining_started",
                cell_id=cell_id,
                reason=reason,
            )

            # 상태 전이 시 양방향 카운터 리셋 (유령 카운터 방지)
            cell.metadata["evacuation_below_count"] = 0
            cell.metadata["recovery_above_count"] = 0

            # 영향 서비스 목록 기록
            cell.metadata["evacuation_affected_services"] = list(cell.assigned_services)
            cell.metadata["evacuation_trigger_score"] = health_score

            # SoT: CellRegistry 상태 전환
            registry.set_cell_state(cell_id, CellState.DRAINING, reason)

            # DRAINING 전환 시점을 즉시 기록 — _tick_draining()이
            # 첫 tick에서 시간을 기록하는 지연 없이 바로 드레인 타이머 시작
            cell.metadata["last_state_change_time"] = time.time()

            # 신규 트래픽 차단은 CellRegistry.get_cell_for_key()의
            # Hash Ring 순회에서 DRAINING Cell이 자동 skip됨으로써 달성된다.
            logger.info(
                "cell_evacuation.draining_confirmed",
                cell_id=cell_id,
            )

            self._emit_event(
                EventType.CELL_EVACUATION_STARTED,
                {
                    "cell_id": cell_id,
                    "reason": reason,
                },
            )

            # 대피 이력 기록
            self._evacuation_history.append(
                EvacuationRecord(
                    cell_id=cell_id,
                    trigger_health_score=health_score,
                    reason=reason,
                    affected_services=list(cell.assigned_services),
                )
            )
            return True
        # 임계치 초과 — 카운터 리셋
        if cell.metadata.get("evacuation_below_count", 0) > 0:
            cell.metadata["evacuation_below_count"] = 0
        return False

    def _check_global_evacuation_limit(
        self,
        cell_id: str,
        registry: CellRegistry,
    ) -> bool:
        """
        전체 Cell 중 격리 비율이 max_evacuated_ratio를 초과하는지 확인.

        Cascading Failure 방지를 위해 일정 비율 이상 격리를 거부한다.

        Returns:
            True면 대피 진행 가능, False면 리미트 초과로 거부.
        """
        total_cells = len(registry.get_all_cells())
        if total_cells == 0:
            return False

        active_cells = len(registry.get_active_cells())
        evacuated_ratio = 1.0 - (active_cells / total_cells)

        if evacuated_ratio >= self._settings.max_evacuated_ratio:
            logger.critical(
                "cell_evacuation.global_limit_reached",
                cell_id=cell_id,
                evacuated_ratio=evacuated_ratio,
                max_evacuated_ratio=self._settings.max_evacuated_ratio,
                active_cells=active_cells,
                total_cells=total_cells,
            )
            return False

        return True

    # =========================================================================
    # State: DRAINING — 드레인 시간 경과 기반 ISOLATED 전환
    # =========================================================================

    def _tick_draining(
        self,
        cell_id: str,
        cell: CellInfo,
        registry: CellRegistry,
        CellState: type[_CellState],
    ) -> bool:
        """
        DRAINING 상태 Cell의 드레인 시간 경과 확인.

        metadata['last_state_change']의 시간값과 현재 시각을 비교하여,
        drain_seconds + grace_buffer가 경과했으면 ISOLATED로 전환한다.

        리더 교체 시에도 Redis L2에 동기화된 metadata를 읽어
        파이프라인을 안전하게 재개할 수 있다.
        """
        last_change = cell.metadata.get("last_state_change", {})
        if last_change.get("to") != CellState.DRAINING.value:
            # metadata가 없거나 불일치 — DRAINING이 외부에서 취소됨
            logger.warning(
                "cell_evacuation.metadata_mismatch",
                cell_id=cell_id,
            )
            self._emit_event(
                EventType.CELL_EVACUATION_CANCELLED,
                {
                    "cell_id": cell_id,
                    "reason": "metadata_mismatch",
                },
            )
            return False

        # 시간 경과 판단 — time.time() + Grace Buffer
        drain_started = cell.metadata.get("last_state_change_time")
        if drain_started is None:
            # 시간 기록이 없으면 현재 시각 기록 후 다음 tick 대기
            cell.metadata["last_state_change_time"] = time.time()
            return False

        elapsed = time.time() - drain_started
        required = (
            self._settings.evacuation_traffic_drain_seconds
            + self._settings.evacuation_drain_grace_seconds
        )

        if elapsed < required:
            logger.debug(
                "cell_evacuation.drain_timer_waiting",
                cell_id=cell_id,
                elapsed=elapsed,
                required=required,
            )
            return False

        # === 드레인 완료 — ISOLATED 전환 ===
        reason = f"Drain period elapsed ({elapsed:.1f}s >= {required:.1f}s)"
        logger.info(
            "cell_evacuation.drain_completed",
            cell_id=cell_id,
        )

        # 양방향 카운터 리셋
        cell.metadata["evacuation_below_count"] = 0
        cell.metadata["recovery_above_count"] = 0

        # SoT: CellRegistry 상태 전환 (가장 먼저 실행)
        registry.set_cell_state(cell_id, CellState.ISOLATED, reason)

        self._emit_event(
            EventType.CELL_EVACUATION_COMPLETED,
            {
                "cell_id": cell_id,
            },
        )

        # Fire-and-forget: 감사 로그 및 이벤트 발행
        self._notify_isolation_gate(
            cell_id,
            reason,
            duration_seconds=self._settings.isolation_notification_duration_seconds,
        )
        self._notify_blast_radius(
            cell_id,
            cell.metadata.get("evacuation_affected_services", []),
        )

        # 서비스 재배치 로깅
        affected = cell.metadata.get("evacuation_affected_services", [])
        logger.info(
            "cell_topology.cell_isolated_services_redistributed",
            cell_id=cell_id,
            services_count=len(affected),
        )
        for svc in affected:
            new_cell = registry.get_cell_for_key(svc)
            logger.info(
                "cell_topology.service_redistributed",
                service=svc,
                from_cell=cell_id,
                to_cell=new_cell,
            )

        return True

    # =========================================================================
    # State: ISOLATED — 히스테리시스 기반 자동 복구
    # =========================================================================

    def _tick_isolated(
        self,
        cell_id: str,
        health_score: float,
        cell: CellInfo,
        registry: CellRegistry,
        CellState: type[_CellState],
    ) -> bool:
        """
        ISOLATED 상태 Cell의 자동 복구 판단.

        히스테리시스: 연속 recovery_consecutive_count(기본 5)회
        recovery_health_threshold(기본 0.7) 이상일 때만 ACTIVE로 복구.
        비대칭 설계: 대피(3회)는 빠르게, 복구(5회)는 보수적으로.
        """
        recovery_threshold = self._settings.recovery_health_threshold

        if health_score >= recovery_threshold:
            above_count = cell.metadata.get("recovery_above_count", 0) + 1
            cell.metadata["recovery_above_count"] = above_count

            if above_count < self._settings.recovery_consecutive_count:
                logger.debug(
                    "cell_evacuation.health_above_threshold",
                    cell_id=cell_id,
                    health_score=health_score,
                    recovery_threshold=recovery_threshold,
                    above_count=above_count,
                    required_count=self._settings.recovery_consecutive_count,
                )
                return False

            # === 연속 카운터 도달 — ACTIVE 복구 ===
            reason = (
                f"Health score {health_score:.2f} >= {recovery_threshold} "
                f"for {above_count} consecutive ticks"
            )
            logger.info(
                "cell_evacuation.restoring_started",
                cell_id=cell_id,
                reason=reason,
            )

            # 상태 전이 시 양방향 카운터 리셋
            cell.metadata["evacuation_below_count"] = 0
            cell.metadata["recovery_above_count"] = 0

            # SoT: CellRegistry 상태 전환
            registry.set_cell_state(cell_id, CellState.ACTIVE, reason)

            # Fire-and-forget: 감사 로그 통보
            self._notify_restore_region(cell_id)

            # 대피 이력 완료 기록 (최신순 역방향 탐색)
            self._complete_evacuation_record(cell_id)

            logger.info(
                "cell_evacuation.cell_restored",
                cell_id=cell_id,
            )
            self._emit_event(
                EventType.CELL_RESTORED,
                {
                    "cell_id": cell_id,
                    "trigger": "auto",
                },
            )
            return True
        # 임계치 미달 — 카운터 리셋
        if cell.metadata.get("recovery_above_count", 0) > 0:
            cell.metadata["recovery_above_count"] = 0
        return False

    # =========================================================================
    # 수동 복구
    # =========================================================================

    def restore_cell(self, cell_id: str) -> bool:
        """
        Cell 수동 복구.

        자동 복구 히스테리시스를 무시하고 즉시 ACTIVE로 전환합니다.
        관리자 개입 시 사용.

        Args:
            cell_id: Cell 식별자

        Returns:
            복원 성공 여부
        """
        if not self._settings.enabled:
            return False

        try:
            from baldur.services.cell_topology import get_cell_registry
            from baldur.services.cell_topology.models import CellState

            registry = get_cell_registry()
            cell = registry.get_cell_info(cell_id)
            if not cell:
                return False

            old_state = cell.state

            # 양방향 카운터 리셋
            cell.metadata["evacuation_below_count"] = 0
            cell.metadata["recovery_above_count"] = 0

            # SoT: CellRegistry 상태 전환
            registry.set_cell_state(cell_id, CellState.ACTIVE, "Manual restoration")

            # DRAINING 중 수동 복구 → evacuation 취소 이벤트
            if old_state == CellState.DRAINING:
                self._emit_event(
                    EventType.CELL_EVACUATION_CANCELLED,
                    {
                        "cell_id": cell_id,
                        "reason": "manual_restore",
                    },
                )

            # Fire-and-forget: 감사 로그 통보
            self._notify_restore_region(cell_id)

            logger.info(
                "cell_evacuation.cell_restored",
                cell_id=cell_id,
            )
            self._emit_event(
                EventType.CELL_RESTORED,
                {
                    "cell_id": cell_id,
                    "trigger": "manual",
                },
            )
            return True

        except Exception as e:
            logger.exception(
                "cell.manual_restore_failed",
                cell_id=cell_id,
                error=e,
            )
            return False

    # =========================================================================
    # Fire-and-forget 통보 — Celery apply_async
    # =========================================================================

    def _notify_isolation_gate(
        self,
        cell_id: str,
        reason: str,
        *,
        duration_seconds: int = 3600,
    ) -> None:
        """RegionalIsolationGate 격리 통보 (Fire-and-forget)."""
        try:
            from baldur.adapters.celery.tasks import (
                notify_cell_isolation,
            )

            notify_cell_isolation.apply_async(
                kwargs={
                    "cell_id": cell_id,
                    "reason": reason,
                    "duration_seconds": duration_seconds,
                },
            )
        except ImportError:
            # Celery 미사용: 동기 폴백
            self._notify_isolation_gate_sync(
                cell_id,
                reason,
                duration_seconds=duration_seconds,
            )
        except Exception as e:
            logger.warning(
                "cell_policy.isolation_gate_notify_failed",
                cell_id=cell_id,
                method="async",
                error=e,
            )
            self._notify_isolation_gate_sync(
                cell_id,
                reason,
                duration_seconds=duration_seconds,
            )

    def _notify_isolation_gate_sync(
        self,
        cell_id: str,
        reason: str,
        *,
        duration_seconds: int = 3600,
    ) -> None:
        """RegionalIsolationGate 동기 폴백."""
        try:
            from baldur.services.isolation.regional_gate import (
                get_regional_isolation_gate,
            )

            gate = get_regional_isolation_gate()
            gate.isolate_region(
                region=cell_id,
                reason=reason,
                duration_seconds=duration_seconds,
            )
        except ImportError:
            logger.debug("cell_policy.region_isolation_gate_unavailable")
        except Exception as e:
            logger.warning(
                "cell_policy.isolation_gate_notify_failed",
                cell_id=cell_id,
                method="sync",
                error=e,
            )

    def _notify_blast_radius(self, cell_id: str, affected_services: list[str]) -> None:
        """BlastRadiusService 정책 설정 통보 (Fire-and-forget)."""
        try:
            from baldur.adapters.celery.tasks import (
                notify_cell_blast_radius,
            )

            notify_cell_blast_radius.apply_async(
                kwargs={
                    "cell_id": cell_id,
                    "affected_services": affected_services,
                },
            )
        except ImportError:
            self._notify_blast_radius_sync(cell_id, affected_services)
        except Exception as e:
            logger.warning(
                "cell_policy.blast_radius_notify_failed",
                cell_id=cell_id,
                method="async",
                error=e,
            )
            self._notify_blast_radius_sync(cell_id, affected_services)

    def _notify_blast_radius_sync(
        self, cell_id: str, affected_services: list[str]
    ) -> None:
        """BlastRadiusService 동기 폴백."""
        try:
            from baldur.services.blast_radius.models import (
                BlastRadiusLevel,
            )
            from baldur.services.blast_radius.service import (
                BlastRadiusService,
            )

            blast_service = BlastRadiusService()
            blast_service.set_policy(
                service_name=cell_id,
                level=BlastRadiusLevel.CRITICAL,
                affected_services=affected_services,
                max_affected_percentage=0.0,
                auto_isolate=True,
            )
        except ImportError:
            pass
        except Exception as e:
            logger.warning(
                "cell_policy.blast_radius_notify_failed",
                cell_id=cell_id,
                method="sync",
                error=e,
            )

    def _notify_restore_region(self, cell_id: str) -> None:
        """RegionalIsolationGate 복구 통보 (Fire-and-forget)."""
        try:
            from baldur.adapters.celery.tasks import (
                notify_cell_restoration,
            )

            notify_cell_restoration.apply_async(
                kwargs={"cell_id": cell_id},
            )
        except ImportError:
            self._notify_restore_region_sync(cell_id)
        except Exception as e:
            logger.warning(
                "cell_policy.restore_region_notify_failed",
                cell_id=cell_id,
                method="async",
                error=e,
            )
            self._notify_restore_region_sync(cell_id)

    def _notify_restore_region_sync(self, cell_id: str) -> None:
        """RegionalIsolationGate 복구 동기 폴백."""
        try:
            from baldur.services.isolation.regional_gate import (
                get_regional_isolation_gate,
            )

            gate = get_regional_isolation_gate()
            gate.restore_region(cell_id)
        except ImportError:
            logger.debug("cell_policy.region_isolation_gate_unavailable")
        except Exception as e:
            logger.warning(
                "cell_policy.restore_region_notify_failed",
                cell_id=cell_id,
                method="sync",
                error=e,
            )

    # =========================================================================
    # 내부 유틸리티
    # =========================================================================

    def _complete_evacuation_record(self, cell_id: str) -> None:
        """Cell 대피 이력에서 해당 cell_id의 미완료 레코드를 완료로 표시."""
        for record in reversed(self._evacuation_history):
            if record.cell_id == cell_id and record.completed_at is None:
                record.completed_at = utc_now()
                break

    # =========================================================================
    # 조회
    # =========================================================================

    def get_evacuation_history(self) -> list[EvacuationRecord]:
        """대피 이력."""
        return list(self._evacuation_history)


# =============================================================================
# Singleton
# =============================================================================

_policy: CellEvacuationPolicy | None = None
_policy_lock = threading.Lock()


def get_cell_evacuation_policy() -> CellEvacuationPolicy:
    """CellEvacuationPolicy 싱글톤 반환."""
    global _policy
    if _policy is None:
        with _policy_lock:
            if _policy is None:
                _policy = CellEvacuationPolicy()
    return _policy


def reset_cell_evacuation_policy() -> None:
    """싱글톤 초기화 (테스트용)."""
    global _policy
    with _policy_lock:
        _policy = None
