"""
Regional Isolation Gate.

리전 단위 트래픽 차단 게이트.

특정 리전(클러스터 그룹)이 불안정할 때
해당 리전으로의 트래픽을 전역적으로 차단.

Audit Integration (85_AUDIT_INTEGRATION_OVERVIEW.md Phase 1):
- 리전 격리: log_region_isolation_audit (action="isolate")
- 리전 복원: log_region_isolation_audit (action="restore")

코드 근거:
- blast_radius.py#L40: REGION 레벨 이미 존재
- guard.py#L827-836: 전역 차단 패턴 존재

Reference: docs/baldur/middleware_system/70_MULTI_CLUSTER_ARCHITECTURE.md
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog

from baldur.audit.helpers import log_region_isolation_audit
from baldur.core.serializable import SerializableMixin
from baldur.utils.serialization import fast_dumps_str, fast_loads
from baldur.utils.time import utc_now

if TYPE_CHECKING:
    from baldur.core.cluster_identity import ClusterIdentity

logger = structlog.get_logger()


@dataclass
class IsolationInfo(SerializableMixin):
    """리전 격리 정보."""

    region: str
    isolated: bool
    reason: str
    isolated_at: datetime | None = None
    isolated_by: str | None = None
    expires_at: datetime | None = None


class RegionalIsolationGate:
    """
    리전 단위 트래픽 차단 게이트.

    특정 리전(클러스터 그룹)이 불안정할 때
    해당 리전으로의 트래픽을 전역적으로 차단.

    Usage:
        gate = get_regional_isolation_gate()

        # 리전 격리
        gate.isolate_region("tokyo", reason="High error rate", duration_seconds=300)

        # 격리 상태 확인
        is_isolated, reason = gate.is_region_isolated("tokyo")
        if is_isolated:
            return redirect_to_fallback()

        # 격리 해제
        gate.restore_region("tokyo")
    """

    # Redis 키 패턴
    GATE_KEY_TEMPLATE = "baldur:global:isolation:{region}"
    ISOLATION_LIST_KEY = "baldur:global:isolation:list"

    # 이벤트 채널
    ISOLATION_EVENT_CHANNEL = "baldur:global:isolation:events"

    def __init__(
        self,
        global_redis: Any | None = None,
        cluster_identity: ClusterIdentity | None = None,
    ):
        """
        Initialize RegionalIsolationGate.

        Args:
            global_redis: 글로벌 Redis 클라이언트
            cluster_identity: 클러스터 식별 정보
        """
        self._redis = global_redis
        self._identity = cluster_identity
        self._initialized = False

    def _ensure_initialized(self) -> None:
        """지연 초기화 수행."""
        if self._initialized:
            return

        # ClusterIdentity 초기화
        if self._identity is None:
            try:
                from baldur.core.cluster_identity import get_cluster_identity

                self._identity = get_cluster_identity()
            except Exception as e:
                logger.warning(
                    "regional_isolation_gate.get_cluster_identity_failed",
                    error=e,
                )

        # Redis 클라이언트 초기화
        if self._redis is None:
            try:
                from baldur.core.tiered_redis import (
                    RedisScope,
                    TieredRedisProvider,
                )

                provider = TieredRedisProvider()
                self._redis = provider.get_redis(RedisScope.GLOBAL)
            except Exception as e:
                logger.warning(
                    "regional_isolation_gate.initialize_redis_failed",
                    error=e,
                )

        self._initialized = True

    def isolate_region(
        self,
        region: str,
        reason: str,
        duration_seconds: int = 300,
    ) -> bool:
        """
        리전 격리 활성화.

        Args:
            region: 격리할 리전
            reason: 격리 사유
            duration_seconds: 격리 지속 시간 (초, 기본 5분)

        Returns:
            격리 성공 여부
        """
        self._ensure_initialized()

        if not self._redis:
            logger.warning("regional_isolation_gate.redis_available")
            return False

        try:
            key = self.GATE_KEY_TEMPLATE.format(region=region)
            now = utc_now()
            expires_at = datetime.fromtimestamp(
                now.timestamp() + duration_seconds, tz=UTC
            )

            operator = self._identity.cluster_id if self._identity else "unknown"

            isolation_info = IsolationInfo(
                region=region,
                isolated=True,
                reason=reason,
                isolated_at=now,
                isolated_by=operator,
                expires_at=expires_at,
            )

            # 저장
            self._redis.set(
                key, fast_dumps_str(isolation_info.to_dict()), ex=duration_seconds
            )

            # 목록에 추가
            self._redis.sadd(self.ISOLATION_LIST_KEY, region)

            # 이벤트 발행
            self._publish_event("isolated", isolation_info)

            logger.warning(
                "cell_evacuation.cell_isolated",
                target_region=region,
                reason=reason,
                duration_seconds=duration_seconds,
                operator=operator,
            )

            # === Audit 기록: 리전 격리 (85_AUDIT_INTEGRATION Phase 1) ===
            log_region_isolation_audit(
                region=region,
                action="isolate",
                result="success",
                reason=reason,
                duration_seconds=duration_seconds,
                operator=operator,
            )

            return True

        except Exception as e:
            logger.exception(
                "regional_isolation_gate.isolate_region_failed",
                target_region=region,
                error=e,
            )

            # === Audit 기록: 리전 격리 실패 ===
            log_region_isolation_audit(
                region=region,
                action="isolate",
                result="failed",
                reason=reason,
                duration_seconds=duration_seconds,
                operator=self._identity.cluster_id if self._identity else "unknown",
                details={"error": str(e)},
            )

            return False

    def is_region_isolated(self, region: str) -> tuple[bool, str | None]:
        """
        리전 격리 상태 확인.

        Args:
            region: 확인할 리전

        Returns:
            (격리 여부, 격리 사유) 튜플
        """
        self._ensure_initialized()

        if not self._redis:
            return False, None

        try:
            key = self.GATE_KEY_TEMPLATE.format(region=region)
            data = self._redis.get(key)

            if data:
                if isinstance(data, bytes):
                    data = data.decode()
                info = IsolationInfo.from_dict(fast_loads(data))
                return info.isolated, info.reason

            return False, None

        except Exception as e:
            logger.exception(
                "regional_isolation_gate.check_isolation_status_failed",
                error=e,
            )
            return False, None

    def get_isolation_info(self, region: str) -> IsolationInfo | None:
        """
        리전 격리 상세 정보 조회.

        Args:
            region: 조회할 리전

        Returns:
            격리 정보 또는 None
        """
        self._ensure_initialized()

        if not self._redis:
            return None

        try:
            key = self.GATE_KEY_TEMPLATE.format(region=region)
            data = self._redis.get(key)

            if data:
                if isinstance(data, bytes):
                    data = data.decode()
                return IsolationInfo.from_dict(fast_loads(data))

            return None

        except Exception as e:
            logger.exception(
                "regional_isolation_gate.get_isolation_info_failed",
                error=e,
            )
            return None

    def restore_region(self, region: str) -> bool:
        """
        리전 격리 해제.

        Args:
            region: 해제할 리전

        Returns:
            해제 성공 여부
        """
        self._ensure_initialized()

        if not self._redis:
            return False

        operator = self._identity.cluster_id if self._identity else "unknown"

        try:
            key = self.GATE_KEY_TEMPLATE.format(region=region)

            # 기존 정보 조회
            existing = self.get_isolation_info(region)

            # 삭제
            deleted = self._redis.delete(key)
            self._redis.srem(self.ISOLATION_LIST_KEY, region)

            if deleted:
                # 이벤트 발행
                restore_info = IsolationInfo(
                    region=region,
                    isolated=False,
                    reason="Manual restore",
                    isolated_by=operator,
                )
                self._publish_event("restored", restore_info)

                logger.info(
                    "regional_isolation_gate.region_restored",
                    target_region=region,
                )

                # === Audit 기록: 리전 복원 (85_AUDIT_INTEGRATION Phase 1) ===
                log_region_isolation_audit(
                    region=region,
                    action="restore",
                    result="success",
                    reason="Manual restore",
                    operator=operator,
                    details={
                        "previous_reason": existing.reason if existing else None,
                        "was_isolated_by": existing.isolated_by if existing else None,
                    },
                )

                return True

            return False

        except Exception as e:
            logger.exception(
                "regional_isolation_gate.restore_region_failed",
                target_region=region,
                error=e,
            )

            # === Audit 기록: 리전 복원 실패 ===
            log_region_isolation_audit(
                region=region,
                action="restore",
                result="failed",
                reason="Manual restore",
                operator=operator,
                details={"error": str(e)},
            )

            return False

    def list_isolated_regions(self) -> dict[str, IsolationInfo]:
        """
        현재 격리 중인 모든 리전 목록.

        Returns:
            {region: IsolationInfo} 딕셔너리
        """
        self._ensure_initialized()

        if not self._redis:
            return {}

        try:
            regions = self._redis.smembers(self.ISOLATION_LIST_KEY)
            result = {}

            for region in regions:
                if isinstance(region, bytes):
                    region = region.decode()

                info = self.get_isolation_info(region)
                if info and info.isolated:
                    result[region] = info
                else:
                    # 만료된 항목 정리
                    self._redis.srem(self.ISOLATION_LIST_KEY, region)

            return result

        except Exception as e:
            logger.exception(
                "regional_isolation_gate.list_isolated_regions_failed",
                error=e,
            )
            return {}

    def _publish_event(self, event_type: str, info: IsolationInfo) -> None:
        """격리 이벤트 발행."""
        if not self._redis:
            return

        try:
            event = {
                "type": event_type,
                "info": info.to_dict(),
                "timestamp": utc_now().isoformat(),
            }
            self._redis.publish(self.ISOLATION_EVENT_CHANNEL, fast_dumps_str(event))
        except Exception as e:
            logger.exception(
                "regional_isolation_gate.publish_event_failed",
                error=e,
            )

    def is_current_region_isolated(self) -> tuple[bool, str | None]:
        """
        현재 클러스터가 속한 리전의 격리 상태 확인.

        Returns:
            (격리 여부, 격리 사유) 튜플
        """
        self._ensure_initialized()

        if not self._identity or not self._identity.region:
            return False, None

        return self.is_region_isolated(self._identity.region)


# =============================================================================
# Singleton
# =============================================================================

_gate: RegionalIsolationGate | None = None
_gate_lock = threading.Lock()


def get_regional_isolation_gate() -> RegionalIsolationGate:
    """RegionalIsolationGate 싱글톤 반환."""
    global _gate
    if _gate is None:
        with _gate_lock:
            if _gate is None:
                _gate = RegionalIsolationGate()
    return _gate


def reset_regional_isolation_gate() -> None:
    """테스트용 싱글톤 리셋."""
    global _gate
    _gate = None
