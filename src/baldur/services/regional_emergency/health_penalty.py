"""
Emergency Health Penalty.

Emergency 상태에 따른 Health Score 감점 계산.

주요 기능:
- calculate_penalty(namespace): Emergency 상태에 따른 감점 계산
- get_health_score_with_emergency(base_score, namespace): 감점 적용된 점수 반환
- get_penalty_breakdown(namespace): 감점 상세 내역 (대시보드용)

감점 가중치:
- Regional STRICT: -20점
- Global STRICT: -30점

PropagationHealthMonitor와 통합하여 Emergency 상태가
Health Score에 자동 반영됩니다.

Code reference:
    services/config/propagation_health.py (감점 패턴)
    services/regional_emergency/tracker.py (NamespacedEmergencyTracker)

Reference:
    docs/baldur/middleware_system/73_NAMESPACE_AWARE_EMERGENCY.md
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

import structlog

from baldur.core.serializable import SerializableMixin
from baldur.models.emergency import EmergencyScope
from baldur.utils.time import utc_now

logger = structlog.get_logger()


# =============================================================================
# Constants — documented defaults (actual values loaded from settings at init)
# =============================================================================

REGIONAL_STRICT_PENALTY: float = 20.0
"""Regional STRICT 모드 감점: default -20점."""

GLOBAL_STRICT_PENALTY: float = 30.0
"""Global STRICT 모드 감점: default -30점."""

LEVEL_1_PENALTY: float = 5.0
"""LEVEL_1 (경고 수준) 감점: default -5점."""

LEVEL_2_PENALTY: float = 10.0
"""LEVEL_2 (주의 수준) 감점: default -10점 (STRICT 미적용 시)."""


@dataclass
class PenaltyBreakdown(SerializableMixin):
    """
    감점 상세 내역.

    대시보드에서 "왜 점수가 떨어졌는지" 표시용.
    """

    penalty: float = 0.0
    """적용된 감점 (-점수)."""

    reason: str | None = None
    """감점 사유 (예: 'Global STRICT active since 2026-01-22T...')."""

    scope: str | None = None
    """Emergency 적용 범위 ('global' 또는 'regional')."""

    emergency_level: str = "NORMAL"
    """Emergency 레벨 이름."""

    governance_mode: str = "NORMAL"
    """Governance 모드 ('NORMAL' 또는 'STRICT')."""

    activated_by: str | None = None
    """Emergency 활성화 주체."""

    activated_at: str | None = None
    """Emergency 활성화 시각."""

    namespace: str | None = None
    """대상 네임스페이스."""

    calculated_at: str = field(default_factory=lambda: utc_now().isoformat())
    """계산 시각."""


class EmergencyHealthPenalty:
    """
    Emergency 상태에 따른 Health Score 감점 계산기.

    Emergency 상태가 활성화되면 Health Score에 감점을 적용합니다.
    PropagationHealthMonitor와 통합하여 사용됩니다.

    감점 가중치:
    - LEVEL_1: -5점 (경고 수준)
    - LEVEL_2: -10점 (STRICT 미적용 시)
    - Regional STRICT: -20점
    - Global STRICT: -30점

    Usage:
        penalty = EmergencyHealthPenalty()

        # 감점 계산
        points = penalty.calculate_penalty("seoul")

        # 감점 적용
        adjusted_score = penalty.get_health_score_with_emergency(
            base_score=95.0,
            namespace="seoul"
        )

        # 감점 상세 (대시보드용)
        breakdown = penalty.get_penalty_breakdown("seoul")
    """

    def __init__(
        self,
        tracker: Any | None = None,
        regional_penalty: float | None = None,
        global_penalty: float | None = None,
    ):
        """
        EmergencyHealthPenalty 초기화.

        Args:
            tracker: NamespacedEmergencyTracker 인스턴스 (None이면 자동 획득)
            regional_penalty: Regional STRICT 감점 (None이면 settings에서 로드)
            global_penalty: Global STRICT 감점 (None이면 settings에서 로드)
        """
        self._tracker = tracker
        self._lock = threading.Lock()

        # Settings에서 penalty 값 로드 (인스턴스 생성 시점)
        try:
            from baldur.settings.emergency_mode import get_emergency_mode_settings

            s = get_emergency_mode_settings()
            self._regional_penalty = (
                regional_penalty
                if regional_penalty is not None
                else s.penalty_regional_strict
            )
            self._global_penalty = (
                global_penalty
                if global_penalty is not None
                else s.penalty_global_strict
            )
            self._level_1_penalty = s.penalty_level_1
            self._level_2_penalty = s.penalty_level_2
            self._cache_ttl_seconds = s.penalty_cache_ttl_seconds
        except Exception:
            self._regional_penalty = (
                regional_penalty
                if regional_penalty is not None
                else REGIONAL_STRICT_PENALTY
            )
            self._global_penalty = (
                global_penalty if global_penalty is not None else GLOBAL_STRICT_PENALTY
            )
            self._level_1_penalty = LEVEL_1_PENALTY
            self._level_2_penalty = LEVEL_2_PENALTY
            self._cache_ttl_seconds = 5.0

        # 캐시 (빈번한 호출 최적화)
        self._cached_penalty: dict[str, float] = {}
        self._cache_timestamp: dict[str, float] = {}

    def _get_tracker(self) -> Any:
        """NamespacedEmergencyTracker 인스턴스 획득."""
        if self._tracker is None:
            from baldur.services.regional_emergency.tracker import (
                get_namespaced_emergency_tracker,
            )

            self._tracker = get_namespaced_emergency_tracker()
        return self._tracker

    def calculate_penalty(self, namespace: str | None = None) -> float:
        """
        현재 Emergency 상태에 따른 감점 계산.

        감점 기준:
        - Global STRICT: -30점
        - Regional STRICT: -20점
        - LEVEL_2 (non-STRICT): -10점
        - LEVEL_1: -5점
        - NORMAL: 0점

        Args:
            namespace: 대상 네임스페이스 (None이면 현재 인스턴스)

        Returns:
            감점 점수 (0 이상, 양수)
        """
        import time

        ns = namespace or "global"
        cache_key = f"penalty:{ns}"
        now = time.time()

        # 캐시 확인
        with self._lock:
            if cache_key in self._cached_penalty:
                cache_time = self._cache_timestamp.get(cache_key, 0)
                if now - cache_time < self._cache_ttl_seconds:
                    return self._cached_penalty[cache_key]

        # Emergency 상태 조회
        tracker = self._get_tracker()
        state = tracker.get_effective_state(namespace=namespace)

        # 감점 계산
        penalty = 0.0

        if not state.is_active:
            penalty = 0.0
        elif state.governance_mode == "STRICT":
            if state.scope == EmergencyScope.GLOBAL:
                penalty = self._global_penalty
            else:
                penalty = self._regional_penalty
        else:
            # STRICT는 아니지만 Emergency 활성화 상태
            # LEVEL에 따른 경감 감점
            level_severity = getattr(state.emergency_level, "severity", 0)
            if level_severity >= 2:
                penalty = self._level_2_penalty
            elif level_severity >= 1:
                penalty = self._level_1_penalty

        # 캐시 저장
        with self._lock:
            self._cached_penalty[cache_key] = penalty
            self._cache_timestamp[cache_key] = now

        logger.debug(
            "emergency_health_penalty.event",
            namespace_id=ns,
            penalty=penalty,
            governance_mode=state.governance_mode,
        )

        return penalty

    def get_health_score_with_emergency(
        self,
        base_score: float,
        namespace: str | None = None,
    ) -> float:
        """
        Emergency 감점이 반영된 Health Score 반환.

        Args:
            base_score: 기본 Health Score (0-100)
            namespace: 대상 네임스페이스

        Returns:
            감점 적용된 Health Score (0-100, 클램프됨)
        """
        penalty = self.calculate_penalty(namespace=namespace)
        adjusted_score = base_score - penalty

        # 클램프: 0-100 범위
        result = max(0.0, min(100.0, adjusted_score))

        if penalty > 0:
            logger.debug(
                "emergency_health_penalty.applied_penalty",
                base_score=base_score,
                penalty=penalty,
                health_result=result,
            )

        return result

    def get_penalty_breakdown(
        self,
        namespace: str | None = None,
    ) -> PenaltyBreakdown:
        """
        감점 상세 내역 반환.

        대시보드에서 "왜 점수가 떨어졌는지" 표시용.

        Args:
            namespace: 대상 네임스페이스

        Returns:
            PenaltyBreakdown 인스턴스
        """
        tracker = self._get_tracker()
        state = tracker.get_effective_state(namespace=namespace)
        penalty = self.calculate_penalty(namespace=namespace)

        if not state.is_active or penalty == 0:
            return PenaltyBreakdown(
                penalty=0.0,
                reason=None,
                scope=None,
                emergency_level="NORMAL",
                governance_mode="NORMAL",
                namespace=namespace,
            )

        # 상세 사유 생성
        scope_str = (
            state.scope.value if hasattr(state.scope, "value") else str(state.scope)
        )
        level_name = getattr(state.emergency_level, "name", str(state.emergency_level))

        reason = (
            f"Emergency {scope_str.upper()} {state.governance_mode} active "
            f"(Level: {level_name})"
        )
        if state.activated_at:
            reason += f" since {state.activated_at}"

        return PenaltyBreakdown(
            penalty=penalty,
            reason=reason,
            scope=scope_str,
            emergency_level=level_name,
            governance_mode=state.governance_mode,
            activated_by=state.activated_by,
            activated_at=state.activated_at,
            namespace=namespace,
        )

    def invalidate_cache(self, namespace: str | None = None) -> None:
        """
        캐시 무효화.

        Emergency 상태 변경 시 호출하여 캐시 갱신.

        Args:
            namespace: 특정 네임스페이스만 무효화 (None이면 전체)
        """
        with self._lock:
            if namespace:
                cache_key = f"penalty:{namespace}"
                self._cached_penalty.pop(cache_key, None)
                self._cache_timestamp.pop(cache_key, None)
            else:
                self._cached_penalty.clear()
                self._cache_timestamp.clear()

        logger.debug(
            "emergency_health_penalty.cache_invalidated",
            target_namespace=namespace or "all",
        )


# =============================================================================
# Singleton
# =============================================================================

_health_penalty: EmergencyHealthPenalty | None = None
_health_penalty_lock = threading.Lock()


def get_emergency_health_penalty() -> EmergencyHealthPenalty:
    """EmergencyHealthPenalty 싱글톤 반환."""
    global _health_penalty
    if _health_penalty is None:
        with _health_penalty_lock:
            if _health_penalty is None:
                _health_penalty = EmergencyHealthPenalty()
    return _health_penalty


def reset_emergency_health_penalty() -> None:
    """
    싱글톤 초기화 (테스트용).

    테스트 간 격리를 위해 싱글톤 인스턴스를 제거합니다.
    """
    global _health_penalty
    with _health_penalty_lock:
        _health_penalty = None
