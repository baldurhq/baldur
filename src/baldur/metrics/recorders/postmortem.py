"""
Postmortem metric recorder — metric definitions and recording.

Owns all postmortem-related Prometheus metrics.
"""

from __future__ import annotations

import structlog

from baldur.metrics.recorders.base import BaseMetricRecorder
from baldur.metrics.registry import get_or_create_counter

logger = structlog.get_logger()

__all__ = [
    "PostmortemMetricRecorder",
    "record_postmortem_generated",
]


class PostmortemMetricRecorder(BaseMetricRecorder):
    """Postmortem metric definitions and recording."""

    def __init__(self) -> None:
        self._generated_total = get_or_create_counter(
            f"{self.PREFIX}_postmortem_generated_total",
            "Postmortem generation count by type",
            ["type"],
        )

    def record_generated(self, postmortem_type: str) -> None:
        """Record a postmortem generation.

        postmortem_type: auto|group|emergency
        """
        try:
            self._generated_total.labels(type=postmortem_type).inc()
        except Exception as e:
            logger.warning("metrics.record_postmortem_generated_failed", error=e)


# --- Module-level convenience functions (DD-7) ---


def _lazy_recorder() -> PostmortemMetricRecorder | None:
    try:
        from baldur.metrics.prometheus import get_metrics

        return getattr(get_metrics(), "postmortem", None)
    except Exception:
        return None


def record_postmortem_generated(postmortem_type: str) -> None:
    rec = _lazy_recorder()
    if rec:
        rec.record_generated(postmortem_type)
