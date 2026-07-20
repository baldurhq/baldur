"""
Dashboard Service - Data Models

Distribution, AlertInfo, DashboardSummary dataclasses.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from baldur.interfaces.statistics import (
    RecentActivity,
    StatusCounts,
)


@dataclass
class Distribution:
    """Distribution data by domain and failure type."""

    by_domain: list[dict[str, Any]] = field(default_factory=list)
    by_failure_type: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class AlertInfo:
    """Alert information."""

    high_retry_count: int = 0
    avg_retry_count: float = 0.0


@dataclass
class DashboardSummary:
    """Complete dashboard summary data."""

    timestamp: str
    health_status: str
    status_counts: StatusCounts
    recent_activity: RecentActivity
    distribution: Distribution
    alerts: AlertInfo
    resolution_rate_percent: float = 0.0
    recommendations: list[str] = field(default_factory=list)
    # Recovery Coordinator integration
    recovery_summary: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for API response."""
        result = {
            "timestamp": self.timestamp,
            "health_status": self.health_status,
            "overview": {
                "total": self.status_counts.total,
                "pending": self.status_counts.pending,
                "resolved": self.status_counts.resolved,
                "failed": self.status_counts.failed,
                "archived": self.status_counts.archived,
                "resolution_rate_percent": self.resolution_rate_percent,
            },
            "recent_activity": {
                "new_failures_24h": self.recent_activity.new_in_24h,
                "resolved_24h": self.recent_activity.resolved_in_24h,
                "new_failures_7d": self.recent_activity.new_in_7d,
                "resolved_7d": self.recent_activity.resolved_in_7d,
            },
            "distribution": {
                "by_domain": self.distribution.by_domain,
                "by_failure_type": self.distribution.by_failure_type,
            },
            "alerts": {
                "high_retry_count": self.alerts.high_retry_count,
                "avg_retry_count": self.alerts.avg_retry_count,
            },
            "recommendations": self.recommendations,
        }
        # Add the recovery summary when present
        if self.recovery_summary:
            result["recovery"] = self.recovery_summary
        return result
