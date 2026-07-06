"""
Cascade Load Shedding - 고부하 시 이벤트 드롭 관리.

Audit 시스템이 고부하 상황에서 시스템 전체 장애로 번지지 않도록
우선순위 기반 Load Shedding을 적용합니다.

Features:
- 우선순위 기반 이벤트 드롭 (LOW → MEDIUM 순)
- 버퍼 사용률 기반 자동 조절
- CRITICAL 이벤트는 절대 드롭하지 않음
- 메트릭 기록

Usage:
    from baldur.audit.cascade_load_shedding import CascadeLoadShedding

    shedding = CascadeLoadShedding()

    # 이벤트 기록 전 확인
    decision = shedding.should_accept(
        trigger_type="METRICS_UPDATED",
        buffer_size=8000,
        buffer_capacity=10000,
    )

    if decision["accepted"]:
        # 이벤트 기록
        auditor.record(...)
    else:
        # 드롭 또는 로컬 폴백
        shedding.record_dropped(trigger_type)

Reference:
    docs/baldur/middleware_system/76_CASCADE_EVENT_AUDIT.md
    services/circuit_breaker/load_shedding.py (패턴 참조)
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

import structlog

from baldur.audit.cascade_config import (
    AuditBackpressureConfig,
    get_audit_backpressure_config,
)
from baldur.audit.cascade_event import (
    CascadeEventPriority,
    get_priority_for_trigger,
)
from baldur.utils.time import utc_now

logger = structlog.get_logger()


# =============================================================================
# Metrics (Prometheus 호환)
# =============================================================================


@dataclass
class LoadSheddingMetrics:
    """Load Shedding 메트릭."""

    accepted_count: int = 0
    """수락된 이벤트 수."""

    dropped_count: int = 0
    """드롭된 이벤트 수."""

    fallback_count: int = 0
    """폴백으로 처리된 이벤트 수."""

    dropped_by_priority: dict[str, int] = field(
        default_factory=lambda: {
            "LOW": 0,
            "MEDIUM": 0,
            "HIGH": 0,
            "CRITICAL": 0,
        }
    )
    """우선순위별 드롭 수."""

    last_shedding_time: str | None = None
    """마지막 Load Shedding 발생 시각."""

    def to_dict(self) -> dict[str, Any]:
        """딕셔너리 변환."""
        return {
            "accepted_count": self.accepted_count,
            "dropped_count": self.dropped_count,
            "fallback_count": self.fallback_count,
            "dropped_by_priority": dict(self.dropped_by_priority),
            "last_shedding_time": self.last_shedding_time,
            "drop_rate": (
                self.dropped_count / max(1, self.accepted_count + self.dropped_count)
            ),
        }


# =============================================================================
# CascadeLoadShedding
# =============================================================================


class CascadeLoadShedding:
    """
    Cascade Event Load Shedding 관리자.

    버퍼 사용률에 따라 우선순위가 낮은 이벤트를 드롭합니다.
    CRITICAL 이벤트는 절대 드롭하지 않습니다.

    Shedding Policy:
        - 정상 (< warning_threshold): 모든 이벤트 수락
        - 경고 (warning ~ critical): LOW 우선순위 드롭
        - 임계 (>= critical_threshold): LOW + MEDIUM 드롭
        - CRITICAL 이벤트: 항상 수락 (로컬 폴백 사용)

    Code reference:
        test_lazy_import.py#L105-117 (get_load_shedding_manager 패턴)
        services/circuit_breaker/load_shedding.py
    """

    def __init__(self, config: AuditBackpressureConfig | None = None):
        """
        Args:
            config: Backpressure 설정 (None이면 기본값)
        """
        self.config = config or get_audit_backpressure_config()
        self._metrics = LoadSheddingMetrics()
        self._lock = threading.RLock()

        # Rate limiting
        self._event_timestamps: list[float] = []
        self._rate_window_seconds = self._get_rate_window_seconds()

    @staticmethod
    def _get_rate_window_seconds() -> float:
        """Settings에서 rate_window_seconds 조회."""
        try:
            from baldur.settings.audit import get_audit_settings

            return get_audit_settings().cascade_rate_window_seconds
        except Exception:
            return 1.0  # 기본값

    def should_accept(
        self,
        trigger_type: str,
        buffer_size: int,
        buffer_capacity: int,
        priority: CascadeEventPriority | None = None,
    ) -> dict[str, Any]:
        """
        이벤트 수락 여부 결정.

        Args:
            trigger_type: 트리거 타입
            buffer_size: 현재 버퍼 크기
            buffer_capacity: 버퍼 최대 용량
            priority: 우선순위 (None이면 trigger_type에서 추론)

        Returns:
            결정 결과:
            - accepted: 수락 여부
            - priority: 이벤트 우선순위
            - buffer_ratio: 버퍼 사용률
            - reason: 결정 사유
            - use_fallback: 로컬 폴백 사용 권장 여부
        """
        if not self.config.load_shedding_enabled:
            return {
                "accepted": True,
                "priority": CascadeEventPriority.MEDIUM.name,
                "buffer_ratio": 0.0,
                "reason": "load_shedding_disabled",
                "use_fallback": False,
            }

        # 우선순위 결정
        event_priority = priority or get_priority_for_trigger(trigger_type)

        # 버퍼 사용률 계산
        buffer_ratio = buffer_size / max(1, buffer_capacity)

        # Rate limiting 확인
        rate_exceeded = self._check_rate_limit()

        with self._lock:
            # CRITICAL은 항상 수락 (폴백 권장)
            if event_priority == CascadeEventPriority.CRITICAL:
                self._metrics.accepted_count += 1
                return {
                    "accepted": True,
                    "priority": event_priority.name,
                    "buffer_ratio": buffer_ratio,
                    "reason": "critical_always_accepted",
                    "use_fallback": buffer_ratio
                    >= self.config.buffer_critical_threshold,
                }

            # 버퍼 상태에 따른 결정
            if buffer_ratio >= self.config.buffer_critical_threshold:
                # 임계 상태: MEDIUM 이하 드롭
                if event_priority <= CascadeEventPriority.MEDIUM:
                    return self._drop_event(
                        event_priority, buffer_ratio, "buffer_critical"
                    )

            # Warning state: drop LOW-priority events
            elif (
                buffer_ratio >= self.config.buffer_warning_threshold
                and event_priority <= CascadeEventPriority.LOW
            ):
                return self._drop_event(event_priority, buffer_ratio, "buffer_warning")

            # Rate limit 초과 시
            if rate_exceeded and event_priority <= CascadeEventPriority.LOW:
                return self._drop_event(event_priority, buffer_ratio, "rate_exceeded")

            # 수락
            self._metrics.accepted_count += 1
            self._record_event_time()

            return {
                "accepted": True,
                "priority": event_priority.name,
                "buffer_ratio": buffer_ratio,
                "reason": "accepted",
                "use_fallback": False,
                "load_shedding_triggered": buffer_ratio
                >= self.config.buffer_critical_threshold,
            }

    def _drop_event(
        self,
        priority: CascadeEventPriority,
        buffer_ratio: float,
        reason: str,
    ) -> dict[str, Any]:
        """이벤트 드롭 처리."""
        self._metrics.dropped_count += 1
        self._metrics.dropped_by_priority[priority.name] += 1
        self._metrics.last_shedding_time = utc_now().isoformat()

        logger.warning(
            "cascade_load_shedding.event_dropped",
            priority=priority.name,
            reason=reason,
            buffer_ratio=buffer_ratio,
        )

        return {
            "accepted": False,
            "priority": priority.name,
            "buffer_ratio": buffer_ratio,
            "reason": reason,
            "use_fallback": self.config.fallback_enabled,
        }

    def _check_rate_limit(self) -> bool:
        """
        Rate limit 초과 여부 확인.

        Returns:
            True면 초당 최대 이벤트 수 초과
        """
        now = time.time()
        cutoff = now - self._rate_window_seconds

        with self._lock:
            # 오래된 타임스탬프 제거
            self._event_timestamps = [
                ts for ts in self._event_timestamps if ts > cutoff
            ]

            return len(self._event_timestamps) >= self.config.max_events_per_second

    def _record_event_time(self) -> None:
        """이벤트 시간 기록 (rate limiting용)."""
        self._event_timestamps.append(time.time())

    def record_dropped(
        self,
        trigger_type: str,
        priority: CascadeEventPriority | None = None,
    ) -> None:
        """
        드롭된 이벤트 기록 (메트릭용).

        Args:
            trigger_type: 트리거 타입
            priority: 우선순위
        """
        event_priority = priority or get_priority_for_trigger(trigger_type)

        with self._lock:
            self._metrics.dropped_count += 1
            self._metrics.dropped_by_priority[event_priority.name] += 1

    def record_fallback(self) -> None:
        """폴백 처리 기록."""
        with self._lock:
            self._metrics.fallback_count += 1

    def get_metrics(self) -> dict[str, Any]:
        """현재 메트릭 반환."""
        with self._lock:
            return self._metrics.to_dict()

    def get_status(
        self,
        buffer_size: int,
        buffer_capacity: int,
    ) -> dict[str, Any]:
        """
        현재 Load Shedding 상태 반환.

        Args:
            buffer_size: 현재 버퍼 크기
            buffer_capacity: 버퍼 최대 용량

        Returns:
            상태 정보 딕셔너리
        """
        buffer_ratio = buffer_size / max(1, buffer_capacity)

        if buffer_ratio >= self.config.buffer_critical_threshold:
            status = "CRITICAL"
            shedding_level = "MEDIUM_AND_BELOW"
        elif buffer_ratio >= self.config.buffer_warning_threshold:
            status = "WARNING"
            shedding_level = "LOW_ONLY"
        else:
            status = "NORMAL"
            shedding_level = "NONE"

        return {
            "status": status,
            "buffer_ratio": buffer_ratio,
            "buffer_size": buffer_size,
            "buffer_capacity": buffer_capacity,
            "shedding_level": shedding_level,
            "config": {
                "warning_threshold": self.config.buffer_warning_threshold,
                "critical_threshold": self.config.buffer_critical_threshold,
                "max_events_per_second": self.config.max_events_per_second,
            },
            "metrics": self.get_metrics(),
        }

    def reset_metrics(self) -> None:
        """메트릭 초기화."""
        with self._lock:
            self._metrics = LoadSheddingMetrics()
            self._event_timestamps = []


# =============================================================================
# Singleton
# =============================================================================


_load_shedding: CascadeLoadShedding | None = None
_shedding_lock = threading.Lock()


def get_cascade_load_shedding() -> CascadeLoadShedding:
    """CascadeLoadShedding 싱글톤 반환."""
    global _load_shedding

    if _load_shedding is not None:
        return _load_shedding

    with _shedding_lock:
        if _load_shedding is None:
            _load_shedding = CascadeLoadShedding()
        return _load_shedding


def reset_cascade_load_shedding() -> None:
    """CascadeLoadShedding 싱글톤 리셋. 테스트 용도."""
    global _load_shedding
    with _shedding_lock:
        _load_shedding = None
