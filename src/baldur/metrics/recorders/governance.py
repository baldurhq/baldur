"""
Governance metric recorder — metric definitions and recording.

Owns governance-specific Prometheus metrics (break-glass, cache operations,
pending 4-eyes approval visibility). Emergency mode metrics are handled
separately by EmergencyModeMetricRecorder.
"""

from __future__ import annotations

import structlog

from baldur.metrics.recorders.base import BaseMetricRecorder
from baldur.metrics.registry import get_or_create_counter, get_or_create_gauge

logger = structlog.get_logger()

__all__ = [
    "GovernanceMetricRecorder",
    "record_break_glass_activated",
    "record_governance_cache_operation",
]


class GovernanceMetricRecorder(BaseMetricRecorder):
    """Governance metric definitions and recording."""

    def __init__(self) -> None:
        self._break_glass_total = get_or_create_counter(
            f"{self.PREFIX}_governance_break_glass_total",
            "Break-glass activation count",
            ["reason"],
        )
        self._cache_ops_total = get_or_create_counter(
            f"{self.PREFIX}_governance_cache_operations_total",
            "Governance cache operations",
            ["operation", "result"],
        )
        self._pending_approval_gauge = get_or_create_gauge(
            f"{self.PREFIX}_governance_pending_approval_requests",
            "Count of currently PENDING 4-eyes approval requests",
            [],
        )
        self._oldest_pending_age_gauge = get_or_create_gauge(
            f"{self.PREFIX}_governance_oldest_pending_approval_age_seconds",
            "Age in seconds of the oldest PENDING approval request (0 if none)",
            [],
        )

    def record_break_glass(self, reason: str) -> None:
        """Record a break-glass activation.

        reason: manual|automatic|emergency
        """
        try:
            self._break_glass_total.labels(reason=reason).inc()
        except Exception as e:
            logger.warning("metrics.record_break_glass_failed", error=e)

    def record_cache_operation(self, operation: str, result: str) -> None:
        """Record a governance cache operation.

        operation: get|set|invalidate
        result: hit|miss|success|failure
        """
        try:
            self._cache_ops_total.labels(operation=operation, result=result).inc()
        except Exception as e:
            logger.warning("metrics.record_governance_cache_op_failed", error=e)

    def set_pending_approval_count(self, count: int) -> None:
        """Set the count of currently PENDING approval requests."""
        try:
            self._pending_approval_gauge.set(count)
        except Exception as e:
            logger.warning("metrics.set_pending_approval_count_failed", error=e)

    def set_oldest_pending_approval_age(self, age_seconds: float) -> None:
        """Set the age in seconds of the oldest PENDING approval request."""
        try:
            self._oldest_pending_age_gauge.set(age_seconds)
        except Exception as e:
            logger.warning("metrics.set_oldest_pending_approval_age_failed", error=e)


# --- Module-level convenience functions (DD-7) ---


def _lazy_recorder() -> GovernanceMetricRecorder | None:
    try:
        from baldur.metrics.prometheus import get_metrics

        return getattr(get_metrics(), "governance", None)
    except Exception:
        return None


def record_break_glass_activated(reason: str) -> None:
    rec = _lazy_recorder()
    if rec:
        rec.record_break_glass(reason)


def record_governance_cache_operation(operation: str, result: str) -> None:
    rec = _lazy_recorder()
    if rec:
        rec.record_cache_operation(operation, result)
