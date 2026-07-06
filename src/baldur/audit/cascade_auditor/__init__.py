"""
Cascade Event Auditor - 연계 이벤트 감사기.

연계 이벤트를 생성, 저장, 조회하고 해시 체인 무결성을 검증합니다.

Features:
- Cascade Event 생성 및 저장
- Hash Chain 연결
- 무결성 검증
- 인과관계 조회

Usage:
    from baldur.audit.cascade_auditor import get_cascade_event_auditor

    auditor = get_cascade_event_auditor()

    cascade_event = auditor.record(
        trigger_type="EMERGENCY_LEVEL_CHANGED",
        trigger_details={"old_level": "NORMAL", "new_level": "LEVEL_3"},
        effects=[
            {"action_type": "GOVERNANCE_STRICT", "success": True},
            {"action_type": "CANARY_ROLLBACK", "success": True, "target": "rollout-123"},
        ],
        namespace="seoul",
        triggered_by="system",
    )

    # 조회
    event = auditor.get_cascade_event("cascade-abc123", "seoul")
    events = auditor.get_recent_events("seoul", limit=100)

    # 무결성 검증
    result = auditor.verify_chain_integrity("seoul")
    if result["valid"]:
        print("Hash chain is valid")

Reference:
    docs/baldur/middleware_system/76_CASCADE_EVENT_AUDIT.md
"""

from __future__ import annotations

import threading
from typing import Any

import structlog

from baldur.audit.cascade_auditor._helpers import get_index_ids
from baldur.audit.cascade_auditor._querying import QueryingMixin
from baldur.audit.cascade_auditor._recording import RecordingMixin
from baldur.audit.cascade_auditor._verification import VerificationMixin
from baldur.audit.cascade_auditor._wal_recovery import (
    LOCAL_CASCADE_FALLBACK_PATH,
    LOCAL_CASCADE_WAL_DIR,
    LOCAL_CASCADE_WAL_PATH,
    WALRecoveryMixin,
)
from baldur.settings.cascade_retention import get_cascade_retention_settings

logger = structlog.get_logger()


def _get_max_cascade_index_size() -> int:
    """Get max cascade index size from settings."""
    return get_cascade_retention_settings().max_cascade_index_size


class CascadeEventAuditor(
    RecordingMixin,
    QueryingMixin,
    VerificationMixin,
    WALRecoveryMixin,
):
    """
    Cascade Event 감사기.

    연계 이벤트를 생성, 저장, 조회하고 해시 체인 무결성을 검증합니다.

    Features:
    - Cascade Event 생성 및 저장
    - Hash Chain 연결
    - 무결성 검증
    - 인과관계 조회
    - Load Shedding (Phase 5)
    - Fail-Soft 로컬 폴백 (Phase 5)
    """

    # Redis 키 패턴
    CASCADE_KEY = "baldur:{namespace}:audit:cascade:{cascade_id}"
    CASCADE_INDEX_KEY = "baldur:{namespace}:audit:cascade_index"
    LAST_HASH_KEY = "baldur:{namespace}:audit:cascade_last_hash"

    # Legacy constant for backward compatibility
    MAX_INDEX_SIZE = 10000

    def __init__(
        self,
        enable_load_shedding: bool = True,
        max_index_size: int | None = None,
    ) -> None:
        """
        Args:
            enable_load_shedding: Load Shedding 활성화 여부
            max_index_size: 인덱스 최대 크기 (default from CascadeRetentionSettings)
        """
        self._lock = threading.RLock()
        self._enable_load_shedding = enable_load_shedding
        self._load_shedding = None  # Lazy init
        self._max_index_size = (
            max_index_size
            if max_index_size is not None
            else _get_max_cascade_index_size()
        )

    def _get_backend(self):
        """State backend 획득."""
        from baldur.core.state_backend import get_state_backend

        return get_state_backend()

    def _get_load_shedding(self):
        """Load Shedding 관리자 획득 (Lazy init)."""
        if self._load_shedding is None and self._enable_load_shedding:
            from baldur.audit.cascade_load_shedding import (
                get_cascade_load_shedding,
            )

            self._load_shedding = get_cascade_load_shedding()
        return self._load_shedding

    # =========================================================================
    # Private Methods (Storage)
    # =========================================================================

    def _get_last_hash(self, namespace: str) -> str | None:
        """마지막 해시 조회."""
        backend = self._get_backend()
        key = self.LAST_HASH_KEY.format(namespace=namespace)
        data = backend.get(key)
        if data:
            return data.get("hash") if isinstance(data, dict) else data
        return None

    def _update_last_hash(self, namespace: str, hash_value: str) -> None:
        """마지막 해시 업데이트."""
        backend = self._get_backend()
        key = self.LAST_HASH_KEY.format(namespace=namespace)
        backend.set(key, {"hash": hash_value})

    def _save_cascade_event(self, event: Any) -> None:
        """Cascade Event 저장.

        CascadeRetentionSettings.hot_retention_days를 TTL로 적용하여
        Redis 메모리 누수를 방지한다. StateBackend.set()의 ttl_seconds 파라미터를
        사용하며, RedisStateBackend는 redis.setex()로 원자적 만료를 설정한다.
        """
        backend = self._get_backend()
        key = self.CASCADE_KEY.format(
            namespace=event.namespace,
            cascade_id=event.id,
        )
        retention = get_cascade_retention_settings()
        ttl_seconds = retention.hot_retention_days * 86400
        backend.set(key, event.to_dict(), ttl_seconds=ttl_seconds)

    def _add_to_index(self, namespace: str, cascade_id: str) -> None:
        """인덱스에 추가 (최신순)."""
        backend = self._get_backend()
        key = self.CASCADE_INDEX_KEY.format(namespace=namespace)
        ids = get_index_ids(backend, key)

        # 맨 앞에 추가
        ids.insert(0, cascade_id)

        # 최대 크기 유지
        if len(ids) > self._max_index_size:
            ids = ids[: self._max_index_size]

        backend.set(key, {"ids": ids})


# =============================================================================
# Singleton
# =============================================================================

from baldur.utils.singleton import make_singleton_factory  # noqa: E402

get_cascade_event_auditor, configure_cascade_event_auditor, reset_cascade_auditor = (
    make_singleton_factory("cascade_event_auditor", CascadeEventAuditor)
)

__all__ = [
    "CascadeEventAuditor",
    "get_cascade_event_auditor",
    "configure_cascade_event_auditor",
    "reset_cascade_auditor",
    "LOCAL_CASCADE_WAL_DIR",
    "LOCAL_CASCADE_WAL_PATH",
    "LOCAL_CASCADE_FALLBACK_PATH",
]
