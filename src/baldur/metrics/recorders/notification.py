"""
Unified Notification metric recorder — metric definitions and recording.

Owns all notification-related Prometheus metrics.
"""

from __future__ import annotations

import structlog

from baldur.metrics.recorders.base import BaseMetricRecorder
from baldur.metrics.registry import (
    get_or_create_counter,
    get_or_create_histogram,
)

logger = structlog.get_logger()

__all__ = [
    "NotificationMetricRecorder",
    "record_notification_sent",
    "record_notification_suppressed",
    "observe_notification_duration",
]


class NotificationMetricRecorder(BaseMetricRecorder):
    """Unified Notification metric definitions and recording."""

    def __init__(self) -> None:
        self._sent_total = get_or_create_counter(
            f"{self.PREFIX}_notification_sent_total",
            "Delivery success/failure per channel",
            ["channel", "priority", "result"],
        )
        self._suppressed_total = get_or_create_counter(
            f"{self.PREFIX}_notification_suppressed_total",
            "Cooldown/rate-limit suppression count",
            ["reason"],
        )
        self._duration = get_or_create_histogram(
            f"{self.PREFIX}_notification_duration_seconds",
            "Per-channel send latency",
            ["channel"],
            buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10),
        )

    def record_sent(self, channel: str, priority: str, result: str) -> None:
        """Record a notification delivery attempt.

        channel: slack|email|pagerduty|webhook|...
        priority: critical|high|medium|low
        result: success|failure
        """
        try:
            self._sent_total.labels(
                channel=channel, priority=priority, result=result
            ).inc()
        except Exception as e:
            logger.warning("metrics.record_notification_sent_failed", error=e)

    def record_suppressed(self, reason: str) -> None:
        """Record a suppressed notification.

        reason: cooldown|rate_limit|dedup|...
        """
        try:
            self._suppressed_total.labels(reason=reason).inc()
        except Exception as e:
            logger.warning("metrics.record_notification_suppressed_failed", error=e)

    def observe_duration(self, channel: str, duration: float) -> None:
        """Record per-channel send latency."""
        try:
            self._duration.labels(channel=channel).observe(duration)
        except Exception as e:
            logger.warning("metrics.observe_notification_duration_failed", error=e)


# --- Module-level convenience functions (DD-7) ---


def _lazy_recorder() -> NotificationMetricRecorder | None:
    try:
        from baldur.metrics.prometheus import get_metrics

        return getattr(get_metrics(), "notification", None)
    except Exception:
        return None


def record_notification_sent(channel: str, priority: str, result: str) -> None:
    rec = _lazy_recorder()
    if rec:
        rec.record_sent(channel, priority, result)


def record_notification_suppressed(reason: str) -> None:
    rec = _lazy_recorder()
    if rec:
        rec.record_suppressed(reason)


def observe_notification_duration(channel: str, duration: float) -> None:
    rec = _lazy_recorder()
    if rec:
        rec.observe_duration(channel, duration)
