"""
Propagation Health Monitor.

Health monitoring for global configuration propagation.

Code basis:
- audit/integrity/health_score.py: the IntegrityHealthScore pattern
- settings/propagation.py: the Tier 1/2 SLA definitions

"Global policy consistency" itself becomes a health indicator of the system.
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
    """Health indicators for global configuration propagation."""

    # Latency (ms)
    last_propagation_latency_ms: float = 0.0
    avg_propagation_latency_ms: float = 0.0
    p50_propagation_latency_ms: float = 0.0
    p99_propagation_latency_ms: float = 0.0

    # SLA compliance
    tier1_sla_violations: int = 0  # Times over 1s (Audit/Governance)
    tier2_sla_violations: int = 0  # Times over 30s (Metrics/Stats)
    total_propagations: int = 0

    # Computed score
    propagation_health_score: float = 100.0

    # Timestamps
    calculated_at: str = field(default_factory=lambda: utc_now().isoformat())
    last_propagation_at: str | None = None


@dataclass
class PropagationRecord:
    """A single propagation record."""

    config_type: str
    latency_ms: float
    tier: PropagationTier
    source_cluster: str
    target_cluster: str
    timestamp: datetime


class PropagationHealthMonitor:
    """
    Health monitoring for global configuration propagation.

    Integrates with IntegrityHealthScore to provide a combined HealthScore.

    Penalty rules:
    - Tier 1 SLA violation (>1s): -5 points each
    - Tier 2 SLA violation (>30s): -1 point each

    Prometheus metrics:
    - baldur_propagation_latency_ms (Histogram)
    - baldur_propagation_health_score (Gauge)
    - baldur_propagation_sla_violations_total (Counter)
    """

    # Prometheus metric names
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
            max_history: Maximum number of propagation records to retain
            prometheus_registry: Prometheus registry (optional)
            settings: PropagationSettings instance (auto-resolved if None)
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
        Record a completed propagation.

        Args:
            config_type: Setting type (circuit_breaker, dlq, ...)
            latency_ms: Propagation latency (ms)
            tier: Propagation tier (Tier 1 or Tier 2)
            source_cluster: Source cluster ID
            target_cluster: Target cluster ID
        """
        with self._lock:
            now = utc_now()

            # Append the record
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

            # Check for SLA violations
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
        """Return the current propagation health metrics."""
        with self._lock:
            if not self._latency_history:
                return PropagationHealthMetrics()

            # Compute the statistics
            latencies = sorted(self._latency_history)
            avg_latency = sum(latencies) / len(latencies)
            p50_idx = int(len(latencies) * 0.50)
            p99_idx = min(int(len(latencies) * 0.99), len(latencies) - 1)

            # Compute the HealthScore
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
        Compute the HealthScore.

        Penalty rules:
        - Tier 1 SLA violation: -5 points each
        - Tier 2 SLA violation: -1 point each

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
        Score combined with IntegrityHealthScore.

        Args:
            integrity_score: IntegrityHealthScore (0-100)
            propagation_weight: Propagation weight (default 30%)

        Returns:
            Combined HealthScore (0-100)
        """
        propagation_score = self._calculate_health_score()
        integrity_weight = 1.0 - propagation_weight

        return (integrity_score * integrity_weight) + (
            propagation_score * propagation_weight
        )

    def get_recent_records(self, count: int = 10) -> list[dict[str, Any]]:
        """
        Return the most recent propagation records.

        Args:
            count: Number of records to return

        Returns:
            List of recent records
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
        """Reset every statistic (test use)."""
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
    """Return the PropagationHealthMonitor singleton."""
    global _monitor
    if _monitor is None:
        with _monitor_lock:
            if _monitor is None:
                _monitor = PropagationHealthMonitor()
    return _monitor


def reset_propagation_health_monitor() -> None:
    """Reset for tests."""
    global _monitor
    with _monitor_lock:
        _monitor = None
