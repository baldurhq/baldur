"""
Escalation Audit Trail.

오버라이드 의사결정 이유를 Audit 로그에 박제합니다.

기록 대상:
- Global → Regional 강제 오버라이드
- Admin Override로 Global 무시
- Safety-Max 결정
- Cascade Escalation (다중 리전 연쇄 격상)
- Partition Fallback (네트워크 고립으로 인한 로컬 폴백)

"왜 이 상태가 됐는지" 100% 추적 가능.

Code reference:
    coordination/coordinator.py (DryRunAuditLogger 패턴)
    coordination/critical_path_fallback.py#L214-246 (append_audit_log)

Reference:
    docs/baldur/middleware_system/73_NAMESPACE_AWARE_EMERGENCY.md
"""

from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any

import structlog

from baldur.core.serializable import SerializableMixin
from baldur.utils.time import utc_now

logger = structlog.get_logger()


# =============================================================================
# Decision Types
# =============================================================================


class EscalationDecisionType:
    """
    오버라이드 의사결정 유형.

    각 유형은 "왜 이 상태가 됐는지"를 명시합니다.
    """

    GLOBAL_OVERRIDE = "GLOBAL_OVERRIDE"
    """Global STRICT가 Regional을 강제 오버라이드."""

    ADMIN_OVERRIDE = "ADMIN_OVERRIDE"
    """Admin이 수동으로 Global을 무시하고 Regional 적용."""

    SAFETY_MAX = "SAFETY_MAX"
    """Safety-Max: 둘 중 더 엄격한 상태 선택."""

    REGIONAL_DEFAULT = "REGIONAL_DEFAULT"
    """둘 다 NORMAL, Regional 기본값 사용."""

    CASCADE_ESCALATION = "CASCADE_ESCALATION"
    """다중 리전 연쇄 장애로 인한 Global 격상."""

    PARTITION_FALLBACK = "PARTITION_FALLBACK"
    """네트워크 고립으로 인한 로컬 폴백."""

    REGIONAL_STRICT = "REGIONAL_STRICT"
    """Regional STRICT 활성화 (Global은 NORMAL)."""

    FALLBACK = "FALLBACK"
    """조회 실패로 인한 안전 기본값 사용."""


@dataclass
class EscalationAuditEntry(SerializableMixin):
    """
    오버라이드 의사결정 Audit 엔트리.

    scope와 namespace뿐 아니라 **'왜 이런 결정이 내려졌는지'**를
    명시적으로 기록합니다.

    Attributes:
        event_id: 고유 이벤트 ID (예: "esc-a1b2c3d4e5f6")
        decision_type: 의사결정 유형 (EscalationDecisionType)
        decision_reason: 의사결정 상세 이유
        namespace: 대상 네임스페이스
        effective_state: 최종 적용된 상태
        overridden_state: 덮어씌워진 상태 (Before 스냅샷)
        triggered_by: 결정을 트리거한 주체
        precedence: 명령 우선순위
        timestamp: 기록 시각 (ISO format)
        global_state_snapshot: Global 상태 스냅샷 (결정 시점)
        regional_state_snapshot: Regional 상태 스냅샷 (결정 시점)
        ttl_minutes: Admin Override TTL (분)
    """

    # 고유 식별자
    event_id: str = field(default_factory=lambda: f"esc-{uuid.uuid4().hex[:12]}")

    # 의사결정 정보 (핵심!)
    decision_type: str = ""
    """의사결정 유형 (GLOBAL_OVERRIDE, ADMIN_OVERRIDE, etc.)."""

    decision_reason: str = ""
    """의사결정 이유 (예: 'Global STRICT overrides regional seoul (NORMAL)')."""

    # 상태 정보
    namespace: str = ""
    """대상 네임스페이스."""

    effective_state: dict[str, Any] = field(default_factory=dict)
    """최종 적용된 상태."""

    overridden_state: dict[str, Any] | None = None
    """덮어씌워진 상태 (Before 스냅샷)."""

    # 행위자 정보
    triggered_by: str = ""
    """결정을 트리거한 주체 (user_id, 'system', 'AtomicStateQuery')."""

    precedence: str | None = None
    """명령 우선순위 (수동 오버라이드 시)."""

    # 메타데이터
    timestamp: str = field(default_factory=lambda: utc_now().isoformat())

    # Global 상태 스냅샷 (비교용)
    global_state_snapshot: dict[str, Any] | None = None
    """Global 상태 스냅샷 (결정 시점)."""

    regional_state_snapshot: dict[str, Any] | None = None
    """Regional 상태 스냅샷 (결정 시점)."""

    # TTL 정보
    ttl_minutes: int | None = None
    """Admin Override TTL (분)."""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EscalationAuditEntry:
        """딕셔너리에서 생성."""
        return cls(
            event_id=data.get("event_id", f"esc-{uuid.uuid4().hex[:12]}"),
            decision_type=data.get("decision_type", ""),
            decision_reason=data.get("decision_reason", ""),
            namespace=data.get("namespace", ""),
            effective_state=data.get("effective_state", {}),
            overridden_state=data.get("overridden_state"),
            triggered_by=data.get("triggered_by", ""),
            precedence=data.get("precedence"),
            timestamp=data.get("timestamp", utc_now().isoformat()),
            global_state_snapshot=data.get("global_state_snapshot"),
            regional_state_snapshot=data.get("regional_state_snapshot"),
            ttl_minutes=data.get("ttl_minutes"),
        )


class EscalationAuditTrail:
    """
    오버라이드 의사결정 Audit Trail.

    모든 상태 결정을 기록하여 "왜 이 상태가 됐는지" 100% 추적 가능.

    Features:
    - 메모리 버퍼 + CriticalPathFallback 연동
    - 스레드 안전 (RLock)
    - 의사결정 유형별 편의 메서드

    Code reference:
        coordination/critical_path_fallback.py#L214-246 (append_audit_log)

    Usage:
        audit = EscalationAuditTrail()

        # Global 오버라이드 기록
        event_id = audit.log_global_override(
            namespace="seoul",
            global_state={"governance_mode": "STRICT", ...},
            regional_state={"governance_mode": "NORMAL", ...},
        )

        # 최근 의사결정 조회
        decisions = audit.get_recent_decisions(namespace="seoul", limit=10)
    """

    def __init__(self, max_buffer_size: int | None = None):
        """
        EscalationAuditTrail 초기화.

        Args:
            max_buffer_size: 메모리 버퍼 최대 크기 (None이면 Settings에서 로드)
        """
        self._lock = threading.RLock()
        self._memory_buffer: list[EscalationAuditEntry] = []
        self._max_buffer_size = (
            max_buffer_size
            if max_buffer_size is not None
            else self._get_max_buffer_size()
        )

    @staticmethod
    def _get_max_buffer_size() -> int:
        """Settings에서 max_buffer_size 로드."""
        try:
            from baldur.settings.regional_emergency import (
                get_regional_emergency_settings,
            )

            return get_regional_emergency_settings().max_buffer_size
        except ImportError:
            return 1000  # 기본값

    def log_decision(
        self,
        decision_type: str,
        decision_reason: str,
        namespace: str,
        effective_state: dict[str, Any],
        overridden_state: dict[str, Any] | None = None,
        triggered_by: str = "system",
        precedence: str | None = None,
        global_state: dict[str, Any] | None = None,
        regional_state: dict[str, Any] | None = None,
        ttl_minutes: int | None = None,
    ) -> str:
        """
        의사결정 기록.

        Args:
            decision_type: 의사결정 유형 (EscalationDecisionType)
            decision_reason: 의사결정 이유 (상세!)
            namespace: 대상 네임스페이스
            effective_state: 최종 적용된 상태
            overridden_state: 덮어씌워진 상태 (Before 스냅샷)
            triggered_by: 결정을 트리거한 주체
            precedence: 명령 우선순위
            global_state: Global 상태 스냅샷
            regional_state: Regional 상태 스냅샷
            ttl_minutes: Admin Override TTL

        Returns:
            생성된 event_id
        """
        entry = EscalationAuditEntry(
            decision_type=decision_type,
            decision_reason=decision_reason,
            namespace=namespace,
            effective_state=effective_state,
            overridden_state=overridden_state,
            triggered_by=triggered_by,
            precedence=precedence,
            global_state_snapshot=global_state,
            regional_state_snapshot=regional_state,
            ttl_minutes=ttl_minutes,
        )

        with self._lock:
            self._memory_buffer.append(entry)
            # 버퍼 크기 제한
            if len(self._memory_buffer) > self._max_buffer_size:
                self._memory_buffer = self._memory_buffer[-self._max_buffer_size :]

        # CriticalPathFallback 연동 (영구 저장)
        self._persist_to_fallback(entry)

        # 로그 출력
        log_level = (
            logging.WARNING
            if decision_type
            in (
                EscalationDecisionType.GLOBAL_OVERRIDE,
                EscalationDecisionType.ADMIN_OVERRIDE,
                EscalationDecisionType.CASCADE_ESCALATION,
                EscalationDecisionType.PARTITION_FALLBACK,
            )
            else logging.INFO
        )

        logger.log(
            log_level,
            f"[EscalationAudit] {decision_type}: {decision_reason} "  # noqa: G004
            f"(namespace={namespace}, by={triggered_by})",
        )

        return entry.event_id

    def log_global_override(
        self,
        namespace: str,
        global_state: dict[str, Any],
        regional_state: dict[str, Any],
        triggered_by: str = "system",
    ) -> str:
        """
        Global → Regional 강제 오버라이드 기록.

        Global STRICT가 Regional 상태를 강제로 덮어쓸 때 호출.

        Args:
            namespace: 대상 네임스페이스
            global_state: Global 상태 (적용됨)
            regional_state: Regional 상태 (무시됨)
            triggered_by: 트리거 주체

        Returns:
            생성된 event_id
        """
        reason = (
            f"Global STRICT ({global_state.get('emergency_level', 'N/A')}) "
            f"overrides regional {namespace} "
            f"({regional_state.get('governance_mode', 'NORMAL')})"
        )

        return self.log_decision(
            decision_type=EscalationDecisionType.GLOBAL_OVERRIDE,
            decision_reason=reason,
            namespace=namespace,
            effective_state=global_state,
            overridden_state=regional_state,
            triggered_by=triggered_by,
            global_state=global_state,
            regional_state=regional_state,
        )

    def log_admin_override(
        self,
        namespace: str,
        regional_state: dict[str, Any],
        global_state: dict[str, Any],
        triggered_by: str,
        precedence: str,
        ttl_minutes: int | None = None,
    ) -> str:
        """
        Admin Override 기록 (Global 무시).

        관리자가 명시적으로 Global을 무시하고 Regional 상태를 적용할 때 호출.

        Args:
            namespace: 대상 네임스페이스
            regional_state: Regional 상태 (적용됨)
            global_state: Global 상태 (무시됨)
            triggered_by: 관리자 ID
            precedence: 명령 우선순위 ("ADMIN_OVERRIDE" 또는 "KILL_SWITCH")
            ttl_minutes: 오버라이드 TTL

        Returns:
            생성된 event_id
        """
        reason = (
            f"Admin override ({precedence}) by {triggered_by}: "
            f"using Regional {namespace} ({regional_state.get('governance_mode', 'NORMAL')}) "
            f"instead of Global ({global_state.get('governance_mode', 'NORMAL')})"
        )

        if ttl_minutes:
            reason += f" [TTL: {ttl_minutes}m]"

        return self.log_decision(
            decision_type=EscalationDecisionType.ADMIN_OVERRIDE,
            decision_reason=reason,
            namespace=namespace,
            effective_state=regional_state,
            overridden_state=global_state,
            triggered_by=triggered_by,
            precedence=precedence,
            global_state=global_state,
            regional_state=regional_state,
            ttl_minutes=ttl_minutes,
        )

    def log_regional_strict(
        self,
        namespace: str,
        regional_state: dict[str, Any],
        global_state: dict[str, Any],
        triggered_by: str = "system",
    ) -> str:
        """
        Regional STRICT 활성화 기록.

        Global은 NORMAL이지만 Regional이 STRICT일 때 호출.

        Args:
            namespace: 대상 네임스페이스
            regional_state: Regional 상태 (STRICT)
            global_state: Global 상태 (NORMAL)
            triggered_by: 트리거 주체

        Returns:
            생성된 event_id
        """
        reason = (
            f"Regional STRICT active for {namespace} "
            f"(level={regional_state.get('emergency_level', 'N/A')}), "
            f"Global is NORMAL"
        )

        return self.log_decision(
            decision_type=EscalationDecisionType.REGIONAL_STRICT,
            decision_reason=reason,
            namespace=namespace,
            effective_state=regional_state,
            overridden_state=None,
            triggered_by=triggered_by,
            global_state=global_state,
            regional_state=regional_state,
        )

    def log_cascade_escalation(
        self,
        affected_regions: list[str],
        triggered_by: str = "system",
    ) -> str:
        """
        Cascade Escalation 기록 (다중 리전 연쇄 격상).

        여러 리전이 동시에 STRICT 상태가 되어 Global로 격상할 때 호출.

        Args:
            affected_regions: 영향받은 리전 목록
            triggered_by: 트리거 주체

        Returns:
            생성된 event_id
        """
        reason = (
            f"Cascade escalation to Global STRICT: "
            f"{len(affected_regions)} regions affected ({', '.join(affected_regions)})"
        )

        return self.log_decision(
            decision_type=EscalationDecisionType.CASCADE_ESCALATION,
            decision_reason=reason,
            namespace="global",
            effective_state={"governance_mode": "STRICT", "scope": "global"},
            overridden_state=None,
            triggered_by=triggered_by,
        )

    def log_fallback(
        self,
        namespace: str,
        error: str,
        triggered_by: str = "AtomicStateQuery",
    ) -> str:
        """
        Fallback 기록 (조회 실패로 인한 안전 기본값).

        Args:
            namespace: 대상 네임스페이스
            error: 실패 원인
            triggered_by: 트리거 주체

        Returns:
            생성된 event_id
        """
        reason = f"Query failed for {namespace}, using safe default: {error}"

        return self.log_decision(
            decision_type=EscalationDecisionType.FALLBACK,
            decision_reason=reason,
            namespace=namespace,
            effective_state={"governance_mode": "NORMAL", "is_active": False},
            triggered_by=triggered_by,
        )

    def get_recent_decisions(
        self,
        namespace: str | None = None,
        decision_type: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """
        최근 의사결정 조회.

        Args:
            namespace: 필터링할 네임스페이스 (None이면 전체)
            decision_type: 필터링할 의사결정 유형 (None이면 전체)
            limit: 반환할 최대 개수

        Returns:
            의사결정 목록 (최신순)
        """
        with self._lock:
            entries = self._memory_buffer[-limit:]

            if namespace:
                entries = [e for e in entries if e.namespace == namespace]

            if decision_type:
                entries = [e for e in entries if e.decision_type == decision_type]

            return [e.to_dict() for e in reversed(entries)]

    def get_decision_by_id(self, event_id: str) -> dict[str, Any] | None:
        """
        특정 의사결정 조회.

        Args:
            event_id: 이벤트 ID

        Returns:
            의사결정 딕셔너리 또는 None
        """
        with self._lock:
            for entry in self._memory_buffer:
                if entry.event_id == event_id:
                    return entry.to_dict()
        return None

    def get_stats(self) -> dict[str, Any]:
        """
        Audit Trail 통계 반환.

        Returns:
            통계 딕셔너리
        """
        with self._lock:
            by_type: dict[str, int] = {}
            by_namespace: dict[str, int] = {}

            for entry in self._memory_buffer:
                by_type[entry.decision_type] = by_type.get(entry.decision_type, 0) + 1
                by_namespace[entry.namespace] = by_namespace.get(entry.namespace, 0) + 1

            return {
                "total_entries": len(self._memory_buffer),
                "by_type": by_type,
                "by_namespace": by_namespace,
                "buffer_capacity": self._max_buffer_size,
            }

    def clear(self) -> None:
        """버퍼 초기화 (테스트용)."""
        with self._lock:
            self._memory_buffer.clear()

    def _persist_to_fallback(self, entry: EscalationAuditEntry) -> None:
        """CriticalPathFallback에 영구 저장."""
        try:
            from baldur_pro.services.coordination.critical_path_fallback import (
                CriticalPathFallback,
            )

            fallback = CriticalPathFallback()
            fallback.append_audit_log(entry.to_dict())
        except Exception as e:
            logger.debug(
                "escalation_audit.fallback_persist_skipped",
                error=e,
            )


# =============================================================================
# Singleton
# =============================================================================

_audit_trail: EscalationAuditTrail | None = None
_audit_trail_lock = threading.Lock()


def get_escalation_audit_trail() -> EscalationAuditTrail:
    """
    EscalationAuditTrail 싱글톤 반환.

    Returns:
        EscalationAuditTrail 인스턴스
    """
    global _audit_trail
    if _audit_trail is None:
        with _audit_trail_lock:
            if _audit_trail is None:
                _audit_trail = EscalationAuditTrail()
    return _audit_trail


def reset_escalation_audit_trail() -> None:
    """테스트용 싱글톤 리셋."""
    global _audit_trail
    _audit_trail = None
