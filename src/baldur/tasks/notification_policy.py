"""
NotificationPolicy - Task Notification Policy Configuration

This module provides data classes for configuring task notification policies.
It implements risk-based notification timing, alert fatigue prevention, and
severity management.

Key Components:
- NotificationTiming: When to send notifications (BEFORE/AFTER/REALTIME/AGGREGATED)
- NotificationThreshold: Severity calculation based on metric values
- NotificationPolicy: Complete notification configuration

Usage:
    from baldur.tasks.notification_policy import (
        NotificationPolicy,
        NotificationTiming,
        NotificationThreshold,
    )

    policy = NotificationPolicy(
        timing=NotificationTiming.AFTER,
        default_severity="info",
        threshold=10,
        threshold_field="count",
    )
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Literal


class NotificationTiming(str, Enum):
    """Notification timing strategy based on task risk level."""

    BEFORE = "before"  # Pre-execution notification/approval (high-risk)
    AFTER = "after"  # Post-execution result notification
    REALTIME = "realtime"  # Immediate notification (state changes)
    AGGREGATED = "aggregated"  # Include in daily summary report


@dataclass
class NotificationThreshold:
    """
    Alert threshold configuration.

    Controls when alerts are sent based on metric values:
    - Below log_only: Only log, no notification
    - Between log_only and warning: INFO notification
    - Between warning and critical: WARNING notification
    - Above critical: CRITICAL notification
    """

    log_only: float = 5.0  # Below this: log only
    warning: float = 20.0  # Above this: WARNING
    critical: float = 50.0  # Above this: CRITICAL

    def get_severity(self, value: float) -> str | None:
        """
        Determine severity based on value.

        Args:
            value: The metric value to evaluate

        Returns:
            Severity level or None if no notification needed
        """
        if value >= self.critical:
            return "critical"
        if value >= self.warning:
            return "warning"
        if value < self.log_only:
            return None  # No notification
        return "info"


@dataclass
class NotificationPolicy:
    """
    Task-specific notification policy configuration.

    Attributes:
        timing: When to send notifications (BEFORE/AFTER/REALTIME/AGGREGATED)
        aggregate: If True, include in daily summary instead of immediate notification
        threshold: Numeric threshold - only notify if exceeded
        threshold_field: Result field name to check against threshold
        cooldown_seconds: Minimum seconds between identical alerts
        default_severity: Default severity level for notifications
        channels: Notification channels (default: ["slack"])
        requires_approval: If True, task requires approval before execution (high-risk)
        escalate_on_emergency: If True, escalate timing on Emergency Level >= 3
    """

    timing: NotificationTiming = NotificationTiming.AFTER
    aggregate: bool = False
    threshold: float | None = None
    threshold_field: str = ""
    cooldown_seconds: int = 300  # 5 minutes
    default_severity: Literal["info", "warning", "critical"] = "info"
    channels: list[str] = field(default_factory=lambda: ["slack"])
    requires_approval: bool = False
    escalate_on_emergency: bool = True
