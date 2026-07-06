"""
Regional Cascade Detector.

다중 리전 연쇄 장애 감지 및 GLOBAL 격상 권고.

여러 리전이 동시에 STRICT 상태에 진입하면 전역적 장애 징후로 판단하고
GLOBAL Emergency 격상을 제안합니다.

주요 기능:
- check_cascade_condition(): 연쇄 장애 조건 확인
- get_cascade_status(): 현재 cascade 상태 조회
- auto_escalate_to_global(): 자동 GLOBAL 격상 (설정 시)

Cascade 조건:
- 2개 이상 리전이 동시에 STRICT 상태
- 짧은 시간(window) 내 다중 리전 활성화

Code reference:
    coordination/anti_flapping.py (AntiFlappingGuard 패턴)
    isolation/regional_gate.py (list_isolated_regions 패턴)

Reference:
    docs/baldur/middleware_system/73_NAMESPACE_AWARE_EMERGENCY.md
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import structlog

from baldur.core.serializable import SerializableMixin
from baldur.models.emergency import EmergencyLevel, EmergencyScope
from baldur.utils.time import utc_now

logger = structlog.get_logger()


# =============================================================================
# Constants
# =============================================================================

DEFAULT_ESCALATION_THRESHOLD = 2
"""기본 격상 임계값: 2개 이상 리전이 STRICT면 cascade."""

DEFAULT_CASCADE_WINDOW_MINUTES = 30
"""Cascade 판단 시간 윈도우 (분)."""


def _get_escalation_threshold() -> int:
    """Settings에서 escalation_threshold 로드."""
    try:
        from baldur.settings.regional_emergency import (
            get_regional_emergency_settings,
        )

        return get_regional_emergency_settings().escalation_threshold
    except ImportError:
        return DEFAULT_ESCALATION_THRESHOLD


def _get_cascade_window_minutes() -> int:
    """Settings에서 cascade_window_minutes 로드."""
    try:
        from baldur.settings.regional_emergency import (
            get_regional_emergency_settings,
        )

        return get_regional_emergency_settings().cascade_window_minutes
    except ImportError:
        return DEFAULT_CASCADE_WINDOW_MINUTES


@dataclass
class CascadeDetectionEvent(SerializableMixin):
    """
    Cascade 이벤트 정보.

    연쇄 장애 감지 시 생성되는 이벤트 레코드.
    """

    event_id: str = ""
    """이벤트 고유 ID."""

    detected_at: datetime = field(default_factory=lambda: utc_now())
    """감지 시각."""

    affected_regions: list[str] = field(default_factory=list)
    """영향받은 리전 목록."""

    total_strict_count: int = 0
    """STRICT 상태 리전 수."""

    threshold: int = DEFAULT_ESCALATION_THRESHOLD
    """적용된 임계값."""

    auto_escalated: bool = False
    """자동 GLOBAL 격상 여부."""

    escalated_at: datetime | None = None
    """격상 시각."""

    escalated_by: str = ""
    """격상 주체 ("system" 또는 admin ID)."""


class RegionalCascadeDetector:
    """
    다중 리전 연쇄 장애 감지기.

    여러 리전이 동시에 STRICT 상태면 GLOBAL 격상을 권고합니다.

    설계 원칙:
    - 기본적으로 권고만 (auto_escalate=False)
    - 운영자 확인 후 수동 격상이 안전
    - auto_escalate=True면 자동 GLOBAL 전환 (위험!)

    Usage:
        detector = RegionalCascadeDetector(threshold=2)

        # 주기적 체크 (예: 1분마다)
        result = detector.check_cascade_condition()

        if result["cascade_detected"]:
            print(f"⚠️ Cascade detected: {result['affected_regions']}")
            print(f"Recommendation: {result['recommendation']}")
    """

    def __init__(
        self,
        tracker: Any | None = None,
        escalation_threshold: int | None = None,
        cascade_window_minutes: int | None = None,
        auto_escalate: bool = False,
    ):
        """
        RegionalCascadeDetector 초기화.

        Args:
            tracker: NamespacedEmergencyTracker 인스턴스
            escalation_threshold: STRICT 리전 수 임계값 (None이면 Settings에서 로드)
            cascade_window_minutes: cascade 판단 시간 윈도우 (None이면 Settings에서 로드)
            auto_escalate: True면 자동 GLOBAL 격상 (위험, 기본: False)
        """
        self._tracker = tracker
        self._threshold = (
            escalation_threshold
            if escalation_threshold is not None
            else _get_escalation_threshold()
        )
        self._window_minutes = (
            cascade_window_minutes
            if cascade_window_minutes is not None
            else _get_cascade_window_minutes()
        )
        self._auto_escalate = auto_escalate
        self._lock = threading.Lock()

        # Cascade 이벤트 히스토리 (메모리 버퍼)
        self._cascade_history: list[CascadeDetectionEvent] = []
        self._max_history_size = 100

    def _get_tracker(self) -> Any:
        """NamespacedEmergencyTracker 인스턴스 획득."""
        if self._tracker is None:
            from baldur.services.regional_emergency.tracker import (
                get_namespaced_emergency_tracker,
            )

            self._tracker = get_namespaced_emergency_tracker()
        return self._tracker

    def check_cascade_condition(self) -> dict[str, Any]:
        """
        연쇄 장애 조건 확인.

        Returns:
            dict:
                cascade_detected: cascade 감지 여부
                strict_count: STRICT 상태 리전 수
                affected_regions: 영향받은 리전 목록
                threshold: 적용된 임계값
                recommendation: 권고 사항
                auto_escalated: 자동 격상 수행 여부
                checked_at: 확인 시각
        """
        tracker = self._get_tracker()

        # 활성 네임스페이스 조회
        active_namespaces = tracker.get_all_active_namespaces()

        # Global 제외한 Regional STRICT 리전만 필터
        regional_strict = []
        for ns in active_namespaces:
            if ns == "global":
                continue
            state = tracker.get_state(namespace=ns)
            if state.governance_mode == "STRICT":
                regional_strict.append(ns)

        strict_count = len(regional_strict)
        cascade_detected = strict_count >= self._threshold

        result = {
            "cascade_detected": cascade_detected,
            "strict_count": strict_count,
            "affected_regions": regional_strict,
            "threshold": self._threshold,
            "recommendation": "",
            "auto_escalated": False,
            "checked_at": utc_now().isoformat(),
        }

        if cascade_detected:
            result["recommendation"] = (
                f"⚠️ {strict_count} regions in STRICT mode (threshold: {self._threshold}). "
                "Consider activating GLOBAL emergency mode."
            )

            logger.warning(
                "cascade_detector.cascade_condition_detected",
                regional_strict=regional_strict,
                strict_count=strict_count,
            )

            # Cascade 이벤트 기록
            event = self._record_cascade_event(regional_strict)

            # 자동 격상 (설정 시)
            if self._auto_escalate:
                self._escalate_to_global(regional_strict, event)
                result["auto_escalated"] = True
                result["recommendation"] = (
                    f"🚨 AUTO-ESCALATED to GLOBAL: {strict_count} regions affected"
                )
        else:
            result["recommendation"] = "✅ No cascade condition detected."

        return result

    def get_cascade_status(self) -> dict[str, Any]:
        """
        현재 cascade 상태 조회.

        check_cascade_condition()의 간략 버전.

        Returns:
            dict: cascade 상태 정보
        """
        return self.check_cascade_condition()

    def get_recent_cascade_events(self, limit: int = 10) -> list[dict[str, Any]]:
        """
        최근 cascade 이벤트 조회.

        Args:
            limit: 반환할 최대 개수

        Returns:
            cascade 이벤트 목록 (최신순)
        """
        with self._lock:
            events = self._cascade_history[-limit:]
            return [e.to_dict() for e in reversed(events)]

    def manual_escalate_to_global(
        self,
        escalated_by: str,
        reason: str,
    ) -> dict[str, Any]:
        """
        수동 GLOBAL 격상.

        운영자가 cascade 상태를 확인하고 수동으로 격상할 때 사용.

        Args:
            escalated_by: 격상 주체 (admin ID)
            reason: 격상 사유

        Returns:
            격상 결과
        """
        tracker = self._get_tracker()

        # 현재 상태 확인
        active_regions = tracker.get_all_active_namespaces()
        regional_strict = [
            ns
            for ns in active_regions
            if ns != "global" and tracker.get_state(ns).governance_mode == "STRICT"
        ]

        # GLOBAL 활성화
        state = tracker.activate_emergency(
            level=EmergencyLevel.LEVEL_3,
            activated_by=escalated_by,
            reason=f"Manual cascade escalation: {reason}. Affected: {regional_strict}",
            scope=EmergencyScope.GLOBAL,
        )

        # 이벤트 기록
        event = CascadeDetectionEvent(
            event_id=f"cascade-manual-{utc_now().strftime('%Y%m%d%H%M%S')}",
            affected_regions=regional_strict,
            total_strict_count=len(regional_strict),
            threshold=self._threshold,
            auto_escalated=False,
            escalated_at=utc_now(),
            escalated_by=escalated_by,
        )

        with self._lock:
            self._cascade_history.append(event)
            if len(self._cascade_history) > self._max_history_size:
                self._cascade_history = self._cascade_history[-self._max_history_size :]

        logger.critical(
            "cascade_detector.manual_escalation_global",
            escalated_by=escalated_by,
            regional_strict=regional_strict,
        )

        return {
            "success": True,
            "escalated_to": "GLOBAL",
            "escalated_by": escalated_by,
            "affected_regions": regional_strict,
            "state": state.to_dict(),
        }

    # =========================================================================
    # Private Methods
    # =========================================================================

    def _record_cascade_event(
        self, affected_regions: list[str]
    ) -> CascadeDetectionEvent:
        """Cascade 이벤트 기록."""
        event = CascadeDetectionEvent(
            event_id=f"cascade-{utc_now().strftime('%Y%m%d%H%M%S')}",
            affected_regions=affected_regions,
            total_strict_count=len(affected_regions),
            threshold=self._threshold,
        )

        with self._lock:
            self._cascade_history.append(event)
            if len(self._cascade_history) > self._max_history_size:
                self._cascade_history = self._cascade_history[-self._max_history_size :]

        return event

    def _escalate_to_global(
        self,
        affected_regions: list[str],
        event: CascadeDetectionEvent,
    ) -> None:
        """자동 GLOBAL 격상 (auto_escalate=True일 때만)."""
        tracker = self._get_tracker()

        tracker.activate_emergency(
            level=EmergencyLevel.LEVEL_3,
            activated_by="CascadeDetector",
            reason=f"Auto cascade escalation: {len(affected_regions)} regions affected",
            scope=EmergencyScope.GLOBAL,
        )

        # 이벤트 업데이트
        event.auto_escalated = True
        event.escalated_at = utc_now()
        event.escalated_by = "CascadeDetector"

        # Audit 로그
        try:
            from baldur.services.regional_emergency.escalation_audit import (
                EscalationDecisionType,
                get_escalation_audit_trail,
            )

            audit = get_escalation_audit_trail()
            audit.log_decision(
                decision_type=EscalationDecisionType.CASCADE_ESCALATION,
                decision_reason=f"Auto-escalated: {len(affected_regions)} regions in STRICT",
                namespace="global",
                effective_state={"governance_mode": "STRICT", "scope": "global"},
                triggered_by="CascadeDetector",
            )
        except Exception as e:
            logger.warning(
                "cascade_detector.audit_log_failed",
                error=e,
            )

        logger.critical(
            "cascade_detector.auto_escalated_global_strict",
            affected_regions=affected_regions,
        )


# =============================================================================
# Singleton
# =============================================================================

_cascade_detector: RegionalCascadeDetector | None = None
_detector_lock = threading.Lock()


def get_cascade_detector() -> RegionalCascadeDetector:
    """RegionalCascadeDetector 싱글톤 반환."""
    global _cascade_detector

    if _cascade_detector is None:
        with _detector_lock:
            if _cascade_detector is None:
                _cascade_detector = RegionalCascadeDetector()

    return _cascade_detector


def reset_cascade_detector() -> None:
    """싱글톤 초기화 (테스트용)."""
    global _cascade_detector
    with _detector_lock:
        _cascade_detector = None
