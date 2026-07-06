"""
Corruption Shield metric recorder — manages both Prometheus counters
AND internal _stats dict in a single write path.

shield.validate() → recorder.record_validation() (one call updates both).
shield.get_stats() delegates to recorder.get_stats().

Metrics (2):
- baldur_corruption_shield_validation_total: Validation result counter
- baldur_corruption_shield_violation_total: Per-layer violation counter
"""

from __future__ import annotations

import threading

import structlog

from baldur.metrics.recorders.base import BaseMetricRecorder
from baldur.metrics.registry import get_or_create_counter

logger = structlog.get_logger()

__all__ = ["CorruptionShieldMetricRecorder"]


class CorruptionShieldMetricRecorder(BaseMetricRecorder):
    """Corruption Shield metric definitions and recording (2 metrics + stats)."""

    def __init__(self) -> None:
        self._validation_total = get_or_create_counter(
            f"{self.PREFIX}_corruption_shield_validation_total",
            "Total corruption shield validations by result",
            ["result"],
        )
        self._violation_total = get_or_create_counter(
            f"{self.PREFIX}_corruption_shield_violation_total",
            "Corruption shield violations by layer",
            ["layer"],
        )

        self._stats = {
            "total_validations": 0,
            "passed": 0,
            "blocked": 0,
            "l1_violations": 0,
            "l2_violations": 0,
            "l3_violations": 0,
        }
        self._stats_lock = threading.Lock()

    def record_validation(self, is_valid: bool, blocked: bool) -> None:
        """Record a validation result — updates both Prometheus and internal stats."""
        try:
            result = "passed" if (is_valid and not blocked) else "blocked"
            self._validation_total.labels(result=result).inc()
        except Exception:
            logger.debug("corruption_shield.metric_record_failed", metric="validation")

        with self._stats_lock:
            self._stats["total_validations"] += 1
            if is_valid:
                self._stats["passed"] += 1
            if blocked:
                self._stats["blocked"] += 1

    def record_violation(self, layer: str, count: int = 1) -> None:
        """Record violations for a specific layer — updates both Prometheus and stats."""
        try:
            self._violation_total.labels(layer=layer).inc(count)
        except Exception:
            logger.debug("corruption_shield.metric_record_failed", metric="violation")

        with self._stats_lock:
            key = f"{layer}_violations"
            if key in self._stats:
                self._stats[key] += count

    def get_stats(self) -> dict[str, int]:
        """Return a snapshot of internal stats."""
        with self._stats_lock:
            return dict(self._stats)
