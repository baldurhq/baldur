"""
Dashboard Service

Provides centralized dashboard statistics and monitoring operations.
Extracts business logic from views/dashboard.py for better separation of concerns.

Features:
- System status overview (pending, resolved, failed, archived counts)
- Recent activity tracking (24h, 7d periods)
- Distribution analysis by domain and failure type
- Health status determination
- Resolution rate and retry count statistics
- **Redis caching for high-traffic scenarios**
- **Hybrid storage support via ProviderRegistry**

Configuration:
    DashboardSettings (baldur.settings.dashboard)
"""

from __future__ import annotations

import threading
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import structlog

from baldur.interfaces.statistics import (
    RecentActivity,
    StatusCounts,
)
from baldur.settings.dashboard import get_dashboard_settings
from baldur.utils.time import utc_now

from .models import AlertInfo, DashboardSummary, Distribution

if TYPE_CHECKING:
    from baldur.interfaces.cache_provider import CacheProviderInterface

logger = structlog.get_logger()


# =============================================================================
# Dashboard Service
# =============================================================================


def _get_statistics_repo():
    """Get statistics repository via ProviderRegistry."""
    from baldur.factory import ProviderRegistry

    return ProviderRegistry.get_statistics_repo()


def _has_statistics_adapter() -> bool:
    """Check if statistics adapter is registered."""
    from baldur.factory import ProviderRegistry

    return ProviderRegistry.has_statistics_adapter()


class DashboardService:
    """
    Dashboard statistics service.

    Provides centralized access to system monitoring data and statistics.
    Uses Redis caching to prevent database overload during high-traffic scenarios.

    Cache Strategy:
    - Summary data is cached for configurable seconds (via DashboardSettings)
    - Individual components (status, activity, distribution) use shorter TTL
    - Entries expire by TTL; callers invalidate immediately via
      invalidate_dashboard_cache() after a significant state change

    Usage:
        service = get_dashboard_service()
        summary = service.get_summary()  # Returns cached if available

        # Force fresh data (bypass cache)
        summary = service.get_summary(skip_cache=True)

    Configuration:
        DashboardSettings (baldur.settings.dashboard)
    """

    def __init__(self, cache: CacheProviderInterface | None = None):
        """Initialize DashboardService with optional cache provider."""
        self._stats_repo = None
        self._cache = cache
        self._settings = get_dashboard_settings()

    @property
    def CACHE_PREFIX(self) -> str:
        """Get cache prefix from settings."""
        return self._settings.cache_prefix

    @property
    def CACHE_TTL_SECONDS(self) -> int:
        """Get default cache TTL from settings."""
        return self._settings.cache_ttl_seconds

    @property
    def CACHE_TTL_STATUS(self) -> int:
        """Get status cache TTL from settings."""
        return self._settings.cache_ttl_status

    @property
    def CACHE_TTL_ACTIVITY(self) -> int:
        """Get activity cache TTL from settings."""
        return self._settings.cache_ttl_activity

    @property
    def stats_repo(self):
        """Lazy load the statistics repository."""
        if self._stats_repo is None:
            self._stats_repo = _get_statistics_repo()
        return self._stats_repo

    @property
    def cache(self) -> CacheProviderInterface | None:
        """Get cache provider, creating default if needed."""
        if self._cache is None:
            try:
                from baldur.factory import ProviderRegistry

                self._cache = ProviderRegistry.get_cache()
            except (ImportError, ValueError, Exception):
                pass
        return self._cache

    def _get_cached(self, key: str) -> dict[str, Any] | None:
        """Get cached value by key."""
        if not self.cache:
            return None
        try:
            full_key = f"{self.CACHE_PREFIX}{key}"
            cached = self.cache.get(full_key)
            if cached:
                logger.debug(
                    "dashboard.cache_hit",
                    cache_key=key,
                )
                return cached
        except Exception as e:
            logger.warning(
                "dashboard.cache_read_failed",
                error=e,
            )
        return None

    def _set_cached(
        self, key: str, value: dict[str, Any], ttl_seconds: int | None = None
    ) -> None:
        """Set cached value with TTL."""
        if not self.cache:
            return
        try:
            full_key = f"{self.CACHE_PREFIX}{key}"
            ttl = ttl_seconds or self.CACHE_TTL_SECONDS
            self.cache.set(full_key, value, ttl=timedelta(seconds=ttl))
            logger.debug(
                "dashboard.cache_set",
                cache_key=key,
                ttl=ttl,
            )
        except Exception as e:
            logger.warning(
                "dashboard.cache_write_failed",
                error=e,
            )

    def get_summary(self, skip_cache: bool = False) -> DashboardSummary:
        """
        Get complete dashboard summary.

        Args:
            skip_cache: If True, bypass cache and fetch fresh data

        Returns:
            DashboardSummary: Complete dashboard data
        """
        cache_key = "summary"

        # Try cache first (unless skip_cache)
        if not skip_cache:
            cached = self._get_cached(cache_key)
            if cached:
                return self._dict_to_summary(cached)

        # Fetch fresh data
        current_time = utc_now()

        # Get all component data
        status_counts = self.get_status_counts()
        recent_activity = self.get_recent_activity()
        distribution = self.get_distribution()
        alerts = self.get_alerts()

        # Calculate resolution rate
        resolution_rate = self.calculate_resolution_rate(
            resolved=status_counts.resolved,
            total=status_counts.total,
            archived=status_counts.archived,
        )

        # Determine health status
        health_status = self.determine_health_status(
            pending=status_counts.pending,
            failed=status_counts.failed,
        )

        # Recovery summary (None when the recovery dashboard is unavailable)
        recovery_summary = self._get_recovery_summary()

        summary = DashboardSummary(
            timestamp=current_time.isoformat(),
            health_status=health_status,
            status_counts=status_counts,
            recent_activity=recent_activity,
            distribution=distribution,
            alerts=alerts,
            resolution_rate_percent=resolution_rate,
            recommendations=[],  # Future: add AI recommendations
            recovery_summary=recovery_summary,
        )

        # Cache the result
        self._set_cached(cache_key, summary.to_dict(), self.CACHE_TTL_SECONDS)

        return summary

    def _dict_to_summary(self, data: dict[str, Any]) -> DashboardSummary:
        """Convert cached dictionary back to DashboardSummary."""
        overview = data.get("overview", {})
        recent = data.get("recent_activity", {})
        dist = data.get("distribution", {})
        alerts_data = data.get("alerts", {})

        return DashboardSummary(
            timestamp=data.get("timestamp", ""),
            health_status=data.get("health_status", "unknown"),
            status_counts=StatusCounts(
                total=overview.get("total", 0),
                pending=overview.get("pending", 0),
                resolved=overview.get("resolved", 0),
                failed=overview.get("failed", 0),
                archived=overview.get("archived", 0),
            ),
            recent_activity=RecentActivity(
                new_in_24h=recent.get("new_failures_24h", 0),
                resolved_in_24h=recent.get("resolved_24h", 0),
                new_in_7d=recent.get("new_failures_7d", 0),
                resolved_in_7d=recent.get("resolved_7d", 0),
            ),
            distribution=Distribution(
                by_domain=dist.get("by_domain", []),
                by_failure_type=dist.get("by_failure_type", []),
            ),
            alerts=AlertInfo(
                high_retry_count=alerts_data.get("high_retry_count", 0),
                avg_retry_count=alerts_data.get("avg_retry_count", 0.0),
            ),
            resolution_rate_percent=overview.get("resolution_rate_percent", 0.0),
            recommendations=data.get("recommendations", []),
            recovery_summary=data.get("recovery"),
        )

    def _get_recovery_summary(self) -> dict[str, Any] | None:
        """
        Get recovery system summary.

        Integrates with RecoveryDashboardService to provide
        recovery-related statistics for the main dashboard.

        Returns:
            Recovery summary dict or None if unavailable
        """
        try:
            from baldur_pro.services.coordination.recovery_dashboard import (
                get_recovery_dashboard_service,
            )

            recovery_service = get_recovery_dashboard_service()
            return recovery_service.get_recovery_summary()
        except ImportError:
            logger.debug("dashboard.recovery_service_unavailable")
            return None
        except Exception as e:
            logger.warning(
                "dashboard.get_recovery_summary_failed",
                error=e,
            )
            return None

    def get_status_counts(self) -> StatusCounts:
        """
        Get counts by status.

        Uses ProviderRegistry to access statistics repository,
        which can be Django ORM, SQLAlchemy, or NullStatisticsRepository.

        Returns:
            StatusCounts: Counts for each status
        """
        try:
            stats = self.stats_repo.get_status_counts()
            return StatusCounts(
                total=stats.total,
                pending=stats.pending,
                resolved=stats.resolved,
                failed=stats.failed,
                archived=stats.archived,
            )
        except Exception as e:
            logger.exception(
                "dashboard.status_counts_aggregation_failed",
                error=e,
            )
            return StatusCounts()

    def get_recent_activity(self, hours: int = 24, days: int = 7) -> RecentActivity:
        """
        Get recent activity statistics.

        Uses statistics repository via ProviderRegistry.

        Args:
            hours: Hours for short-term activity (default: 24)
            days: Days for longer-term activity (default: 7)

        Returns:
            RecentActivity: Recent activity data
        """
        try:
            activity = self.stats_repo.get_recent_activity(hours=hours, days=days)
            return RecentActivity(
                new_in_24h=activity.new_in_24h,
                resolved_in_24h=activity.resolved_in_24h,
                new_in_7d=activity.new_in_7d,
                resolved_in_7d=activity.resolved_in_7d,
            )
        except Exception as e:
            logger.exception(
                "dashboard.recent_activity_fetch_failed",
                error=e,
            )
            return RecentActivity()

    def get_distribution(self, limit: int = 10) -> Distribution:
        """
        Get distribution by domain and failure type.

        Uses statistics repository via ProviderRegistry.

        Args:
            limit: Maximum number of items per category (default: 10)

        Returns:
            Distribution: Distribution data
        """
        try:
            domain_dist = self.stats_repo.get_domain_distribution(limit=limit)
            failure_dist = self.stats_repo.get_failure_type_distribution(limit=limit)

            return Distribution(
                by_domain=[{"domain": d.domain, "count": d.count} for d in domain_dist],
                by_failure_type=[
                    {"failure_type": f.failure_type, "count": f.count}
                    for f in failure_dist
                ],
            )
        except Exception as e:
            logger.exception(
                "dashboard.distribution_analysis_failed",
                error=e,
            )
            return Distribution()

    def get_alerts(self, high_retry_threshold: int = 5) -> AlertInfo:
        """
        Get alert information.

        Uses statistics repository via ProviderRegistry.

        Args:
            high_retry_threshold: Threshold for high retry count (default: 5)

        Returns:
            AlertInfo: Alert data
        """
        try:
            avg_retry = self.stats_repo.get_avg_retry_count()

            # High retry count items - need to check via list_entries or custom query
            # For now, estimate based on average
            high_retry_count = 0
            if avg_retry > high_retry_threshold:
                # If average is above threshold, there are likely high retry items
                high_retry_count = int(avg_retry)

            return AlertInfo(
                high_retry_count=high_retry_count,
                avg_retry_count=round(avg_retry, 2),
            )
        except Exception as e:
            logger.exception(
                "dashboard.alert_info_retrieval_failed",
                error=e,
            )
            return AlertInfo()

    def calculate_resolution_rate(
        self,
        resolved: int,
        total: int,
        archived: int,
    ) -> float:
        """
        Calculate resolution rate.

        Args:
            resolved: Number of resolved entries
            total: Total number of entries
            archived: Number of archived entries

        Returns:
            float: Resolution rate as percentage (0.0 - 100.0)
        """
        active_total = total - archived
        if active_total <= 0:
            return 0.0
        return round((resolved / active_total) * 100, 2)

    def determine_health_status(self, pending: int, failed: int) -> str:
        """
        Determine system health status based on pending and failed counts.

        Args:
            pending: Number of pending entries
            failed: Number of failed entries

        Returns:
            str: Health status ('healthy', 'good', 'warning', 'critical')
        """
        if pending == 0 and failed == 0:
            return "healthy"
        if pending <= 10 and failed == 0:
            return "good"
        if pending <= 50 or failed <= 5:
            return "warning"
        return "critical"


# =============================================================================
# Singleton Instance
# =============================================================================

_dashboard_service: DashboardService | None = None
_dashboard_service_lock = threading.Lock()


def get_dashboard_service(
    cache: CacheProviderInterface | None = None,
) -> DashboardService:
    """
    Get the singleton DashboardService instance.

    Args:
        cache: Optional cache provider for Redis caching.
               If not provided, will attempt to use ProviderRegistry.

    Returns:
        DashboardService: The singleton instance
    """
    global _dashboard_service
    if _dashboard_service is None:
        with _dashboard_service_lock:
            if _dashboard_service is None:
                _dashboard_service = DashboardService(cache=cache)
    return _dashboard_service


def reset_dashboard_service() -> None:
    """Clear the singleton DashboardService instance."""
    global _dashboard_service

    with _dashboard_service_lock:
        _dashboard_service = None


def invalidate_dashboard_cache() -> None:
    """
    Invalidate all dashboard cache entries.

    Call this when significant state changes occur that should
    be immediately reflected in the dashboard.
    """
    service = get_dashboard_service()
    if service.cache:
        try:
            # Clear all dashboard cache keys
            for key in ["summary", "status", "activity", "distribution", "alerts"]:
                full_key = f"{service.CACHE_PREFIX}{key}"
                service.cache.delete(full_key)
            logger.info("dashboard.cache_invalidated")
        except Exception as e:
            logger.warning(
                "dashboard.cache_invalidation_failed",
                error=e,
            )
