"""
Cascade Event Audit Prometheus Metrics.

Prometheus 호환 메트릭 정의.

Metrics:
- baldur_cascade_events_total: Cascade Event 총 개수 (by namespace, trigger_type)
- baldur_cascade_effects_total: 연쇄 효과 총 개수 (by namespace, action_type, success)
- baldur_cascade_chain_depth_max: 최대 체인 깊이 (by namespace)
- baldur_cascade_integrity_valid: Hash Chain 무결성 상태 (1=valid, 0=invalid)
- baldur_cascade_load_shedding_dropped_total: Load Shedding으로 드랍된 이벤트 수 (by priority)
- baldur_cascade_fallback_writes_total: 로컬 폴백 저장 횟수

Reference:
    docs/baldur/middleware_system/76_CASCADE_EVENT_AUDIT.md
"""

from __future__ import annotations

import threading
from datetime import datetime
from typing import Any

import structlog

from baldur.utils.time import utc_now

logger = structlog.get_logger()


class CascadeMetrics:
    """
    Cascade Event Audit용 Prometheus 호환 메트릭.

    싱글턴 패턴으로 구현되어 전역에서 동일 인스턴스를 사용합니다.

    Usage:
        from baldur.audit.cascade_metrics import CascadeMetrics

        metrics = CascadeMetrics.get_instance()
        metrics.record_cascade_event("seoul", "EMERGENCY_LEVEL_CHANGED")
        metrics.record_effect("seoul", "governance_strict", success=True)
    """

    _instance: CascadeMetrics | None = None
    _lock = threading.Lock()

    def __init__(self):
        self._metrics_lock = threading.RLock()

        # Counters
        # {namespace: {trigger_type: count}}
        self._cascade_events_total: dict[str, dict[str, int]] = {}
        # {namespace: {action_type: {success: count}}}
        self._cascade_effects_total: dict[str, dict[str, dict[str, int]]] = {}
        # {priority: count}
        self._load_shedding_dropped_total: dict[str, int] = {}

        # 로컬 폴백 저장 횟수
        self._fallback_writes_total: int = 0

        # Gauges
        # {namespace: max_depth}
        self._chain_depth_max: dict[str, int] = {}
        # {namespace: is_valid (1 or 0)}
        self._integrity_valid: dict[str, int] = {}

        # 타임스탬프
        self._last_updated: datetime | None = None

    @classmethod
    def get_instance(cls) -> CascadeMetrics:
        """싱글턴 인스턴스 획득."""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """테스트용: 싱글턴 인스턴스 초기화."""
        with cls._lock:
            cls._instance = None

    # =========================================================================
    # Record Methods
    # =========================================================================

    def record_cascade_event(
        self,
        namespace: str,
        trigger_type: str,
    ) -> None:
        """
        Cascade Event 기록 메트릭.

        Args:
            namespace: 네임스페이스
            trigger_type: 트리거 유형 (EMERGENCY_LEVEL_CHANGED 등)
        """
        with self._metrics_lock:
            if namespace not in self._cascade_events_total:
                self._cascade_events_total[namespace] = {}

            if trigger_type not in self._cascade_events_total[namespace]:
                self._cascade_events_total[namespace][trigger_type] = 0

            self._cascade_events_total[namespace][trigger_type] += 1
            self._last_updated = utc_now()

        logger.debug(
            "cascade_metrics.recorded_event",
            namespace=namespace,
            trigger_type=trigger_type,
        )

    def record_effect(
        self,
        namespace: str,
        action_type: str,
        success: bool,
    ) -> None:
        """
        연쇄 효과 기록 메트릭.

        Args:
            namespace: 네임스페이스
            action_type: 액션 유형 (governance_strict, canary_rollback 등)
            success: 성공 여부
        """
        with self._metrics_lock:
            if namespace not in self._cascade_effects_total:
                self._cascade_effects_total[namespace] = {}

            if action_type not in self._cascade_effects_total[namespace]:
                self._cascade_effects_total[namespace][action_type] = {
                    "success": 0,
                    "failure": 0,
                }

            status_key = "success" if success else "failure"
            self._cascade_effects_total[namespace][action_type][status_key] += 1
            self._last_updated = utc_now()

    def record_chain_depth(
        self,
        namespace: str,
        depth: int,
    ) -> None:
        """
        체인 깊이 기록 (최대값 갱신).

        Args:
            namespace: 네임스페이스
            depth: 현재 체인 깊이
        """
        with self._metrics_lock:
            current_max = self._chain_depth_max.get(namespace, 0)
            if depth > current_max:
                self._chain_depth_max[namespace] = depth
                self._last_updated = utc_now()

    def record_integrity_check(
        self,
        namespace: str,
        is_valid: bool,
    ) -> None:
        """
        Hash Chain 무결성 검증 결과 기록.

        Args:
            namespace: 네임스페이스
            is_valid: 무결성 유효 여부
        """
        with self._metrics_lock:
            self._integrity_valid[namespace] = 1 if is_valid else 0
            self._last_updated = utc_now()

    def record_load_shedding_drop(
        self,
        priority: str,
    ) -> None:
        """
        Load Shedding 드랍 기록.

        Args:
            priority: 이벤트 우선순위 (CRITICAL, HIGH, MEDIUM, LOW)
        """
        with self._metrics_lock:
            if priority not in self._load_shedding_dropped_total:
                self._load_shedding_dropped_total[priority] = 0

            self._load_shedding_dropped_total[priority] += 1
            self._last_updated = utc_now()

    def record_fallback_write(self) -> None:
        """로컬 폴백 저장 기록."""
        with self._metrics_lock:
            self._fallback_writes_total += 1
            self._last_updated = utc_now()

    # =========================================================================
    # Query Methods
    # =========================================================================

    def get_cascade_events_total(self) -> dict[str, dict[str, int]]:
        """Cascade Event 총 개수 조회 (namespace → trigger_type → count)."""
        with self._metrics_lock:
            return dict(self._cascade_events_total)

    def get_effects_total(self) -> dict[str, dict[str, dict[str, int]]]:
        """연쇄 효과 총 개수 조회 (namespace → action_type → success/failure → count)."""
        with self._metrics_lock:
            return dict(self._cascade_effects_total)

    def get_chain_depth_max(self) -> dict[str, int]:
        """네임스페이스별 최대 체인 깊이 조회."""
        with self._metrics_lock:
            return dict(self._chain_depth_max)

    def get_integrity_status(self) -> dict[str, int]:
        """네임스페이스별 무결성 상태 조회 (1=valid, 0=invalid)."""
        with self._metrics_lock:
            return dict(self._integrity_valid)

    def get_load_shedding_dropped(self) -> dict[str, int]:
        """우선순위별 Load Shedding 드랍 개수 조회."""
        with self._metrics_lock:
            return dict(self._load_shedding_dropped_total)

    def get_fallback_writes_total(self) -> int:
        """로컬 폴백 저장 총 횟수."""
        with self._metrics_lock:
            return self._fallback_writes_total

    def get_all_metrics(self) -> dict[str, Any]:
        """
        전체 메트릭 조회.

        Returns:
            모든 메트릭을 포함한 딕셔너리
        """
        with self._metrics_lock:
            return {
                "cascade_events_total": dict(self._cascade_events_total),
                "cascade_effects_total": dict(self._cascade_effects_total),
                "chain_depth_max": dict(self._chain_depth_max),
                "integrity_valid": dict(self._integrity_valid),
                "load_shedding_dropped_total": dict(self._load_shedding_dropped_total),
                "fallback_writes_total": self._fallback_writes_total,
                "last_updated": (
                    self._last_updated.isoformat() if self._last_updated else None
                ),
            }

    def to_prometheus_format(self) -> str:
        """
        Prometheus 텍스트 형식으로 내보내기.

        Returns:
            Prometheus exposition format 문자열
        """
        lines = []

        with self._metrics_lock:
            # baldur_cascade_events_total
            lines.append(
                "# HELP baldur_cascade_events_total Total cascade events recorded"
            )
            lines.append("# TYPE baldur_cascade_events_total counter")
            for namespace, triggers in self._cascade_events_total.items():
                for trigger_type, count in triggers.items():
                    lines.append(
                        f"baldur_cascade_events_total"
                        f'{{namespace="{namespace}",trigger_type="{trigger_type}"}} {count}'
                    )

            # baldur_cascade_effects_total
            lines.append(
                "# HELP baldur_cascade_effects_total Total cascade effects recorded"
            )
            lines.append("# TYPE baldur_cascade_effects_total counter")
            for namespace, actions in self._cascade_effects_total.items():
                for action_type, statuses in actions.items():
                    for status, count in statuses.items():
                        lines.append(
                            f"baldur_cascade_effects_total"
                            f'{{namespace="{namespace}",action_type="{action_type}",'
                            f'status="{status}"}} {count}'
                        )

            # baldur_cascade_chain_depth_max
            lines.append(
                "# HELP baldur_cascade_chain_depth_max Maximum chain depth recorded"
            )
            lines.append("# TYPE baldur_cascade_chain_depth_max gauge")
            for namespace, depth in self._chain_depth_max.items():
                lines.append(
                    f'baldur_cascade_chain_depth_max{{namespace="{namespace}"}} {depth}'
                )

            # baldur_cascade_integrity_valid
            lines.append(
                "# HELP baldur_cascade_integrity_valid Hash chain integrity status (1=valid)"
            )
            lines.append("# TYPE baldur_cascade_integrity_valid gauge")
            for namespace, valid in self._integrity_valid.items():
                lines.append(
                    f'baldur_cascade_integrity_valid{{namespace="{namespace}"}} {valid}'
                )

            # baldur_cascade_load_shedding_dropped_total
            lines.append(
                "# HELP baldur_cascade_load_shedding_dropped_total Events dropped by load shedding"
            )
            lines.append("# TYPE baldur_cascade_load_shedding_dropped_total counter")
            for priority, count in self._load_shedding_dropped_total.items():
                lines.append(
                    f"baldur_cascade_load_shedding_dropped_total"
                    f'{{priority="{priority}"}} {count}'
                )

            # baldur_cascade_fallback_writes_total
            lines.append(
                "# HELP baldur_cascade_fallback_writes_total Total fallback writes"
            )
            lines.append("# TYPE baldur_cascade_fallback_writes_total counter")
            lines.append(
                f"baldur_cascade_fallback_writes_total {self._fallback_writes_total}"
            )

        return "\n".join(lines)


def get_cascade_metrics() -> CascadeMetrics:
    """CascadeMetrics 싱글턴 획득 헬퍼 함수."""
    return CascadeMetrics.get_instance()
