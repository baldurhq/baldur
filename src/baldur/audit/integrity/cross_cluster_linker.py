"""
Cross-Cluster Audit Linker.

각 클러스터의 로컬 해시 체인을 글로벌 앵커로 연결.

설계 원칙:
- 체인은 클러스터별로 독립 (Local Chain) - 성능 보장
- 일일 앵커만 글로벌 저장소에 통합 (Global Anchoring) - 전사 무결성
"""

from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from baldur.core.cluster_identity import ClusterIdentity

from baldur.settings.audit_integrity import get_audit_integrity_settings
from baldur.utils.serialization import fast_dumps_str, fast_loads
from baldur.utils.time import utc_now

logger = structlog.get_logger()


def _get_local_anchor_ttl_days() -> int:
    """Get local anchor TTL from settings."""
    return get_audit_integrity_settings().cross_cluster_local_ttl_days


def _get_global_anchor_ttl_days() -> int:
    """Get global anchor TTL from settings."""
    return get_audit_integrity_settings().cross_cluster_global_ttl_days


@dataclass
class ClusterDailyAnchor:
    """클러스터별 일일 앵커."""

    cluster_id: str
    anchor_date: date
    final_sequence: int
    final_hash: str
    entry_count: int
    created_at: datetime = field(default_factory=lambda: utc_now())

    def to_dict(self) -> dict[str, Any]:
        return {
            "cluster_id": self.cluster_id,
            "date": self.anchor_date.isoformat(),
            "final_sequence": self.final_sequence,
            "final_hash": self.final_hash,
            "entry_count": self.entry_count,
            "created_at": self.created_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ClusterDailyAnchor:
        """딕셔너리에서 ClusterDailyAnchor 생성."""
        return cls(
            cluster_id=data["cluster_id"],
            anchor_date=date.fromisoformat(data["date"]),
            final_sequence=data["final_sequence"],
            final_hash=data["final_hash"],
            entry_count=data["entry_count"],
            created_at=(
                datetime.fromisoformat(data["created_at"])
                if data.get("created_at")
                else utc_now()
            ),
        )

    def compute_anchor_hash(self) -> str:
        """앵커 해시 계산."""
        data = f"{self.cluster_id}:{self.anchor_date}:{self.final_sequence}:{self.final_hash}"
        return hashlib.sha256(data.encode()).hexdigest()


@dataclass
class GlobalDailyAnchor:
    """글로벌 일일 앵커 (모든 클러스터 통합)."""

    anchor_date: date
    cluster_anchors: list[ClusterDailyAnchor]
    global_hash: str = ""
    created_at: datetime = field(default_factory=lambda: utc_now())

    def __post_init__(self) -> None:
        if not self.global_hash:
            self.global_hash = self._compute_global_hash()

    def _compute_global_hash(self) -> str:
        """모든 클러스터 앵커를 결합한 글로벌 해시."""
        if not self.cluster_anchors:
            return hashlib.sha256(b"empty").hexdigest()

        # 클러스터 ID 순으로 정렬하여 결정론적 해시 보장
        sorted_anchors = sorted(self.cluster_anchors, key=lambda a: a.cluster_id)
        combined = ":".join(a.compute_anchor_hash() for a in sorted_anchors)
        return hashlib.sha256(combined.encode()).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "date": self.anchor_date.isoformat(),
            "cluster_count": len(self.cluster_anchors),
            "cluster_anchors": [a.to_dict() for a in self.cluster_anchors],
            "global_hash": self.global_hash,
            "created_at": self.created_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GlobalDailyAnchor:
        """딕셔너리에서 GlobalDailyAnchor 생성."""
        cluster_anchors = [
            ClusterDailyAnchor.from_dict(ca) for ca in data.get("cluster_anchors", [])
        ]
        return cls(
            anchor_date=date.fromisoformat(data["date"]),
            cluster_anchors=cluster_anchors,
            global_hash=data.get("global_hash", ""),
            created_at=(
                datetime.fromisoformat(data["created_at"])
                if data.get("created_at")
                else utc_now()
            ),
        )


class CrossClusterAuditLinker:
    """
    클러스터 간 Audit 체인 연결기.

    하이브리드 전략:
    - Local: 각 클러스터가 독립적인 해시 체인 유지 (성능)
    - Global: 일일 앵커만 글로벌 저장소에 통합 (무결성 증명)
    """

    # Redis 키 패턴
    LOCAL_ANCHOR_KEY_TEMPLATE = "{prefix}audit:anchor:{date}"
    GLOBAL_ANCHOR_KEY_TEMPLATE = "baldur:global:anchor:{date}"
    GLOBAL_ANCHOR_LIST_KEY = "baldur:global:anchor:list"

    # Legacy constants for backward compatibility
    LOCAL_ANCHOR_TTL_DAYS = 90
    GLOBAL_ANCHOR_TTL_DAYS = 365

    def __init__(
        self,
        local_redis: Any | None = None,
        global_redis: Any | None = None,
        cluster_identity: ClusterIdentity | None = None,
        key_prefix: str = "baldur:",
        local_anchor_ttl_days: int | None = None,
        global_anchor_ttl_days: int | None = None,
    ):
        """
        Initialize CrossClusterAuditLinker.

        Args:
            local_redis: 로컬 Redis 클라이언트
            global_redis: 글로벌 Redis 클라이언트 (없으면 local 사용)
            cluster_identity: 클러스터 식별 정보
            key_prefix: Redis 키 프리픽스
            local_anchor_ttl_days: 로컬 앵커 TTL (default from AuditIntegritySettings)
            global_anchor_ttl_days: 글로벌 앵커 TTL (default from AuditIntegritySettings)
        """
        self._local_redis = local_redis
        self._global_redis = global_redis or local_redis
        self._identity = cluster_identity
        self._key_prefix = key_prefix
        self._local_anchor_ttl = (
            local_anchor_ttl_days
            if local_anchor_ttl_days is not None
            else _get_local_anchor_ttl_days()
        )
        self._global_anchor_ttl = (
            global_anchor_ttl_days
            if global_anchor_ttl_days is not None
            else _get_global_anchor_ttl_days()
        )
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
                    "cross_cluster_audit_linker.get_cluster_identity_failed",
                    error=e,
                )

        # Redis 클라이언트 초기화
        if self._local_redis is None:
            try:
                from baldur.core.tiered_redis import (
                    RedisScope,
                    TieredRedisProvider,
                )

                provider = TieredRedisProvider()
                self._local_redis = provider.get_redis(RedisScope.LOCAL)
                if self._global_redis is None:
                    self._global_redis = provider.get_redis(RedisScope.GLOBAL)
            except Exception as e:
                logger.warning(
                    "cross_cluster_audit_linker.initialize_redis_failed",
                    error=e,
                )

        self._initialized = True

    def create_local_anchor(
        self, target_date: date | None = None
    ) -> ClusterDailyAnchor | None:
        """
        로컬 클러스터의 일일 앵커 생성.

        Args:
            target_date: 대상 날짜 (기본: 어제)

        Returns:
            생성된 앵커 또는 None
        """
        self._ensure_initialized()

        if target_date is None:
            target_date = (utc_now() - timedelta(days=1)).date()

        if not self._local_redis:
            logger.warning("cross_cluster_audit_linker.local_redis_available")
            return None

        try:
            # 로컬 체인에서 해당 날짜의 마지막 엔트리 조회
            state_key = f"{self._key_prefix}audit:hash_chain:state"
            state = self._local_redis.hgetall(state_key)

            if not state:
                logger.warning("cross_cluster_audit_linker.no_hash_chain_state")
                return None

            # bytes to str conversion if needed
            def get_state_value(key: str) -> Any:
                value = state.get(key.encode(), state.get(key))
                if isinstance(value, bytes):
                    return value.decode()
                return value

            sequence = int(get_state_value("sequence") or 0)
            previous_hash = get_state_value("previous_hash") or ""

            # 앵커 생성
            cluster_id = self._identity.cluster_id if self._identity else "unknown"
            anchor = ClusterDailyAnchor(
                cluster_id=cluster_id,
                anchor_date=target_date,
                final_sequence=sequence,
                final_hash=previous_hash,
                entry_count=sequence,  # 전체 시퀀스를 엔트리 카운트로 사용
            )

            # 로컬 저장
            anchor_key = self.LOCAL_ANCHOR_KEY_TEMPLATE.format(
                prefix=self._key_prefix, date=target_date.isoformat()
            )
            self._local_redis.set(
                anchor_key,
                fast_dumps_str(anchor.to_dict()),
                ex=self._local_anchor_ttl * 86400,
            )

            logger.info(
                "cross_cluster_audit_linker.created_local_anchor",
                target_date=target_date,
                anchor=anchor.compute_anchor_hash()[:16],
            )
            return anchor

        except Exception as e:
            logger.exception(
                "cross_cluster_audit_linker.create_local_anchor_failed",
                error=e,
            )
            return None

    def get_local_anchor(self, target_date: date) -> ClusterDailyAnchor | None:
        """
        로컬 앵커 조회.

        Args:
            target_date: 대상 날짜

        Returns:
            앵커 또는 None
        """
        self._ensure_initialized()

        if not self._local_redis:
            return None

        try:
            anchor_key = self.LOCAL_ANCHOR_KEY_TEMPLATE.format(
                prefix=self._key_prefix, date=target_date.isoformat()
            )
            data = self._local_redis.get(anchor_key)
            if data:
                if isinstance(data, bytes):
                    data = data.decode()
                return ClusterDailyAnchor.from_dict(fast_loads(data))
            return None
        except Exception as e:
            logger.exception(
                "cross_cluster_audit_linker.get_local_anchor_failed",
                error=e,
            )
            return None

    def submit_to_global(self, anchor: ClusterDailyAnchor) -> bool:
        """
        로컬 앵커를 글로벌 저장소에 제출.

        Args:
            anchor: 로컬 앵커

        Returns:
            제출 성공 여부
        """
        self._ensure_initialized()

        if not self._global_redis:
            logger.warning("cross_cluster_audit_linker.global_redis_available")
            return False

        try:
            # 글로벌 앵커 키
            global_key = self.GLOBAL_ANCHOR_KEY_TEMPLATE.format(
                date=anchor.anchor_date.isoformat()
            )

            # 기존 글로벌 앵커 조회
            existing = self._global_redis.get(global_key)
            if existing:
                if isinstance(existing, bytes):
                    existing = existing.decode()
                global_data = fast_loads(existing)
                global_anchor = GlobalDailyAnchor.from_dict(global_data)
                cluster_anchors = list(global_anchor.cluster_anchors)

                # 중복 체크
                if any(ca.cluster_id == anchor.cluster_id for ca in cluster_anchors):
                    logger.info(
                        "cross_cluster_audit_linker.anchor_already_submitted",
                        anchor=anchor.cluster_id,
                    )
                    return True
                cluster_anchors.append(anchor)
            else:
                cluster_anchors = [anchor]

            # 글로벌 앵커 생성/갱신
            global_anchor = GlobalDailyAnchor(
                anchor_date=anchor.anchor_date,
                cluster_anchors=cluster_anchors,
            )

            self._global_redis.set(
                global_key,
                fast_dumps_str(global_anchor.to_dict()),
                ex=self._global_anchor_ttl * 86400,
            )

            # 앵커 목록에 추가
            self._global_redis.zadd(
                self.GLOBAL_ANCHOR_LIST_KEY,
                {anchor.anchor_date.isoformat(): anchor.anchor_date.toordinal()},
            )

            logger.info(
                "cross_cluster_audit_linker.submitted_global_global_hash",
                anchor=anchor.cluster_id,
                anchor_date=anchor.anchor_date,
                global_anchor=global_anchor.global_hash[:16],
            )
            return True

        except Exception as e:
            logger.exception(
                "cross_cluster_audit_linker.submit_global_failed",
                error=e,
            )
            return False

    def get_global_anchor(self, target_date: date) -> GlobalDailyAnchor | None:
        """
        글로벌 앵커 조회.

        Args:
            target_date: 대상 날짜

        Returns:
            글로벌 앵커 또는 None
        """
        self._ensure_initialized()

        if not self._global_redis:
            return None

        try:
            global_key = self.GLOBAL_ANCHOR_KEY_TEMPLATE.format(
                date=target_date.isoformat()
            )
            data = self._global_redis.get(global_key)
            if data:
                if isinstance(data, bytes):
                    data = data.decode()
                return GlobalDailyAnchor.from_dict(fast_loads(data))
            return None
        except Exception as e:
            logger.exception(
                "cross_cluster_audit_linker.get_global_anchor_failed",
                error=e,
            )
            return None

    def verify_global_integrity(self, target_date: date) -> dict[str, Any]:
        """
        글로벌 앵커 무결성 검증.

        Args:
            target_date: 검증 대상 날짜

        Returns:
            검증 결과
        """
        self._ensure_initialized()

        if not self._global_redis:
            return {"valid": False, "error": "Global Redis not available"}

        try:
            global_anchor = self.get_global_anchor(target_date)

            if not global_anchor:
                return {"valid": False, "error": "Global anchor not found"}

            # 글로벌 해시 재계산
            recomputed = GlobalDailyAnchor(
                anchor_date=target_date,
                cluster_anchors=global_anchor.cluster_anchors,
            )

            stored_hash = global_anchor.global_hash
            computed_hash = recomputed.global_hash

            return {
                "valid": stored_hash == computed_hash,
                "date": target_date.isoformat(),
                "cluster_count": len(global_anchor.cluster_anchors),
                "clusters": [ca.cluster_id for ca in global_anchor.cluster_anchors],
                "stored_hash": stored_hash[:16] + "..." if stored_hash else None,
                "computed_hash": computed_hash[:16] + "..." if computed_hash else None,
            }

        except Exception as e:
            return {"valid": False, "error": str(e)}

    def list_global_anchors(self, limit: int = 30) -> list[str]:
        """
        글로벌 앵커 목록 조회.

        Args:
            limit: 최대 반환 개수

        Returns:
            날짜 문자열 목록 (최신순)
        """
        self._ensure_initialized()

        if not self._global_redis:
            return []

        try:
            # 최신순으로 조회
            dates = self._global_redis.zrevrange(
                self.GLOBAL_ANCHOR_LIST_KEY, 0, limit - 1
            )
            return [d.decode() if isinstance(d, bytes) else d for d in dates]
        except Exception as e:
            logger.exception(
                "cross_cluster_audit_linker.list_global_anchors_failed",
                error=e,
            )
            return []


# =============================================================================
# Singleton
# =============================================================================

_linker: CrossClusterAuditLinker | None = None
_linker_lock = threading.Lock()


def get_cross_cluster_audit_linker() -> CrossClusterAuditLinker:
    """CrossClusterAuditLinker 싱글톤 반환."""
    global _linker
    if _linker is None:
        with _linker_lock:
            if _linker is None:
                _linker = CrossClusterAuditLinker()
    return _linker


def reset_cross_cluster_audit_linker() -> None:
    """테스트용 싱글톤 리셋."""
    global _linker
    _linker = None
