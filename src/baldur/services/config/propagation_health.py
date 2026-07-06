"""
Propagation Health Monitor.

글로벌 설정 전파 건강 모니터링.

코드 근거:
- audit/integrity/health_score.py: IntegrityHealthScore 패턴
- settings/propagation.py: Tier 1/2 SLA 정의

"글로벌 정책 정합성" 자체가 시스템의 건강 지표가 됩니다.

Reference: docs/baldur/middleware_system/70_MULTI_CLUSTER_ARCHITECTURE.md
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import structlog

from baldur.core.serializable import SerializableMixin
from baldur.services.config.propagator import PropagationTier
from baldur.utils.time import utc_now

logger = structlog.get_logger()


@dataclass
class PropagationHealthMetrics(SerializableMixin):
    """글로벌 설정 전파 건강 지표."""

    # Latency (ms)
    last_propagation_latency_ms: float = 0.0
    avg_propagation_latency_ms: float = 0.0
    p50_propagation_latency_ms: float = 0.0
    p99_propagation_latency_ms: float = 0.0

    # SLA 준수
    tier1_sla_violations: int = 0  # 1초 초과 횟수 (Audit/Governance)
    tier2_sla_violations: int = 0  # 30초 초과 횟수 (Metrics/Stats)
    total_propagations: int = 0

    # 계산된 점수
    propagation_health_score: float = 100.0

    # Timestamps
    calculated_at: str = field(default_factory=lambda: utc_now().isoformat())
    last_propagation_at: str | None = None


@dataclass
class PropagationRecord:
    """개별 전파 기록."""

    config_type: str
    latency_ms: float
    tier: PropagationTier
    source_cluster: str
    target_cluster: str
    timestamp: datetime


class PropagationHealthMonitor:
    """
    글로벌 설정 전파 건강 모니터링.

    IntegrityHealthScore와 통합하여 종합 HealthScore 제공.

    감점 기준:
    - Tier 1 SLA 위반 (>1초): -5점/회
    - Tier 2 SLA 위반 (>30초): -1점/회

    Prometheus 메트릭:
    - baldur_propagation_latency_ms (Histogram)
    - baldur_propagation_health_score (Gauge)
    - baldur_propagation_sla_violations_total (Counter)
    """

    # Prometheus 메트릭 이름
    HISTOGRAM_LATENCY = "baldur_propagation_latency_ms"
    GAUGE_HEALTH_SCORE = "baldur_propagation_health_score"
    COUNTER_SLA_VIOLATIONS = "baldur_propagation_sla_violations_total"

    def __init__(
        self,
        max_history: int = 1000,
        prometheus_registry: Any | None = None,
        settings: Any | None = None,
    ):
        """
        Initialize PropagationHealthMonitor.

        Args:
            max_history: 유지할 최대 전파 기록 수
            prometheus_registry: Prometheus 레지스트리 (옵션)
            settings: PropagationSettings instance (None이면 자동 획득)
        """
        if settings is None:
            from baldur.settings.propagation import get_propagation_settings

            settings = get_propagation_settings()
        self._propagation_settings = settings

        self._lock = threading.Lock()
        self._latency_history: deque[float] = deque(maxlen=max_history)
        self._records: deque[PropagationRecord] = deque(maxlen=max_history)
        self._tier1_violations = 0
        self._tier2_violations = 0
        self._total_propagations = 0
        self._last_propagation_at: datetime | None = None
        self._prometheus_registry = prometheus_registry
        self._max_history = max_history

    def record_propagation(
        self,
        config_type: str,
        latency_ms: float,
        tier: PropagationTier,
        source_cluster: str,
        target_cluster: str,
    ) -> None:
        """
        전파 완료 기록.

        Args:
            config_type: 설정 타입 (circuit_breaker, dlq 등)
            latency_ms: 전파 지연 시간 (ms)
            tier: 전파 등급 (Tier 1 또는 Tier 2)
            source_cluster: 소스 클러스터 ID
            target_cluster: 타겟 클러스터 ID
        """
        with self._lock:
            now = utc_now()

            # 기록 추가
            self._latency_history.append(latency_ms)
            self._records.append(
                PropagationRecord(
                    config_type=config_type,
                    latency_ms=latency_ms,
                    tier=tier,
                    source_cluster=source_cluster,
                    target_cluster=target_cluster,
                    timestamp=now,
                )
            )
            self._total_propagations += 1
            self._last_propagation_at = now

            # SLA 위반 체크
            ps = self._propagation_settings
            if tier == PropagationTier.TIER_1_IMMEDIATE:
                if latency_ms > ps.tier1_max_latency_ms:
                    self._tier1_violations += 1
                    logger.warning(
                        "propagation_health.tier_sla_violation_propagation",
                        config_type=config_type,
                        latency_ms=latency_ms,
                        tier1_sla_threshold_ms=ps.tier1_max_latency_ms,
                        source_cluster=source_cluster,
                        target_cluster=target_cluster,
                    )
            elif tier == PropagationTier.TIER_2_EVENTUAL and (
                latency_ms > ps.tier2_max_latency_ms
            ):
                self._tier2_violations += 1
                logger.warning(
                    "propagation_health.tier_sla_violation_propagation",
                    config_type=config_type,
                    latency_ms=latency_ms,
                    tier2_sla_threshold_ms=ps.tier2_max_latency_ms,
                )

            logger.debug(
                "propagation_health.recorded_ms",
                config_type=config_type,
                latency_ms=latency_ms,
                tier=tier.value,
                source_cluster=source_cluster,
                target_cluster=target_cluster,
            )

    def get_current_metrics(self) -> PropagationHealthMetrics:
        """현재 전파 건강 메트릭 반환."""
        with self._lock:
            if not self._latency_history:
                return PropagationHealthMetrics()

            # 통계 계산
            latencies = sorted(self._latency_history)
            avg_latency = sum(latencies) / len(latencies)
            p50_idx = int(len(latencies) * 0.50)
            p99_idx = min(int(len(latencies) * 0.99), len(latencies) - 1)

            # HealthScore 계산
            health_score = self._calculate_health_score()

            return PropagationHealthMetrics(
                last_propagation_latency_ms=latencies[-1] if latencies else 0.0,
                avg_propagation_latency_ms=avg_latency,
                p50_propagation_latency_ms=latencies[p50_idx] if latencies else 0.0,
                p99_propagation_latency_ms=latencies[p99_idx] if latencies else 0.0,
                tier1_sla_violations=self._tier1_violations,
                tier2_sla_violations=self._tier2_violations,
                total_propagations=self._total_propagations,
                propagation_health_score=health_score,
                last_propagation_at=(
                    self._last_propagation_at.isoformat()
                    if self._last_propagation_at
                    else None
                ),
            )

    def _calculate_health_score(self) -> float:
        """
        HealthScore 계산.

        감점 기준:
        - Tier 1 SLA 위반: -5점/회
        - Tier 2 SLA 위반: -1점/회

        Returns:
            Health score (0-100)
        """
        ps = self._propagation_settings
        score = 100.0
        score -= self._tier1_violations * ps.tier1_penalty_points
        score -= self._tier2_violations * ps.tier2_penalty_points
        return max(0.0, min(100.0, score))

    def get_combined_health_score(
        self,
        integrity_score: float,
        propagation_weight: float = 0.3,
    ) -> float:
        """
        IntegrityHealthScore와 결합한 종합 점수.

        Args:
            integrity_score: IntegrityHealthScore (0-100)
            propagation_weight: Propagation 가중치 (기본 30%)

        Returns:
            종합 HealthScore (0-100)
        """
        propagation_score = self._calculate_health_score()
        integrity_weight = 1.0 - propagation_weight

        return (integrity_score * integrity_weight) + (
            propagation_score * propagation_weight
        )

    def get_recent_records(self, count: int = 10) -> list[dict[str, Any]]:
        """
        최근 전파 기록 반환.

        Args:
            count: 반환할 기록 수

        Returns:
            최근 기록 목록
        """
        with self._lock:
            records = list(self._records)[-count:]
            return [
                {
                    "config_type": r.config_type,
                    "latency_ms": r.latency_ms,
                    "tier": r.tier.value,
                    "source_cluster": r.source_cluster,
                    "target_cluster": r.target_cluster,
                    "timestamp": r.timestamp.isoformat(),
                }
                for r in records
            ]

    def reset(self) -> None:
        """모든 통계 초기화 (테스트용)."""
        with self._lock:
            self._latency_history.clear()
            self._records.clear()
            self._tier1_violations = 0
            self._tier2_violations = 0
            self._total_propagations = 0
            self._last_propagation_at = None


# =============================================================================
# Singleton
# =============================================================================

_monitor: PropagationHealthMonitor | None = None
_monitor_lock = threading.Lock()


def get_propagation_health_monitor() -> PropagationHealthMonitor:
    """PropagationHealthMonitor 싱글톤 반환."""
    global _monitor
    if _monitor is None:
        with _monitor_lock:
            if _monitor is None:
                _monitor = PropagationHealthMonitor()
    return _monitor


def reset_propagation_health_monitor() -> None:
    """테스트용 리셋."""
    global _monitor
    with _monitor_lock:
        _monitor = None
