"""
Entitlement metric recorder — subscription status gauges for SRE dashboards.

Two gauges (D8):
- baldur_entitlement_status: 0=missing, 1=invalid, 2=active
- baldur_entitlement_expiry_days: days until expiry (negative = past due)
"""

from __future__ import annotations

import structlog

from baldur.metrics.recorders.base import BaseMetricRecorder
from baldur.metrics.registry import get_or_create_gauge

logger = structlog.get_logger()

__all__ = [
    "EntitlementMetricRecorder",
    "set_entitlement_status",
    "set_entitlement_expiry_days",
]


class EntitlementMetricRecorder(BaseMetricRecorder):
    """Entitlement metric definitions and recording.

    D8: Two gauges for SRE dashboard visibility.
    """

    def __init__(self) -> None:
        self._status = get_or_create_gauge(
            f"{self.PREFIX}_entitlement_status",
            "Entitlement status (0=missing, 1=invalid, 2=active)",
            [],
        )
        self._expiry_days = get_or_create_gauge(
            f"{self.PREFIX}_entitlement_expiry_days",
            "Days until entitlement expiry (negative = past due)",
            [],
        )

    def set_status(self, value: int) -> None:
        """Set entitlement status gauge (0=missing, 1=invalid, 2=active)."""
        try:
            self._status.set(value)
        except Exception as e:
            logger.warning("metrics.set_entitlement_status_failed", error=e)

    def set_expiry_days(self, days: int) -> None:
        """Set days until entitlement expiry."""
        try:
            self._expiry_days.set(days)
        except Exception as e:
            logger.warning("metrics.set_entitlement_expiry_days_failed", error=e)


# --- Module-level convenience functions ---


def _lazy_recorder() -> EntitlementMetricRecorder | None:
    try:
        from baldur.metrics.prometheus import get_metrics

        return getattr(get_metrics(), "entitlement", None)
    except Exception:
        return None


def set_entitlement_status(value: int) -> None:
    """Set entitlement status gauge (0=missing, 1=invalid, 2=active)."""
    rec = _lazy_recorder()
    if rec:
        rec.set_status(value)


def set_entitlement_expiry_days(days: int) -> None:
    """Set days until entitlement expiry."""
    rec = _lazy_recorder()
    if rec:
        rec.set_expiry_days(days)
