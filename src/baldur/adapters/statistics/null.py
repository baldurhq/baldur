"""
Null Statistics Repository.

This is a Null Object implementation of StatisticsRepositoryInterface.
Used when no statistics adapter is registered.

Features:
- Returns empty/default values for all methods
- No errors thrown
- Runtime functionality continues to work
- Dashboards show "Statistics not available" message
"""

from __future__ import annotations

from typing import Any

import structlog

from baldur.interfaces.statistics import (
    CircuitBreakerInfo,
    CircuitBreakerSummary,
    CleanupStats,
    DomainDistribution,
    EntityAuditTrail,
    FailureTypeDistribution,
    PaginatedResult,
    RecentActivity,
    StatisticsRepositoryInterface,
    StatusCounts,
)

logger = structlog.get_logger()


class NullStatisticsRepository(StatisticsRepositoryInterface):
    """
    Null Object implementation of StatisticsRepositoryInterface.

    Returns empty/default values for all methods.
    This allows the system to function without a statistics adapter
    while gracefully degrading dashboard functionality.

    Usage:
        # This is the default when no adapter is registered
        stats_repo = ProviderRegistry.get_statistics_repo()

        # All methods return empty results
        counts = stats_repo.get_status_counts()  # StatusCounts(all zeros)
    """

    _warned: bool = False

    def __init__(self):
        """Initialize NullStatisticsRepository."""
        if not NullStatisticsRepository._warned:
            logger.warning("null_statistics_repository.no_adapter_registered")
            NullStatisticsRepository._warned = True

    # =========================================================================
    # DLQ Statistics
    # =========================================================================

    def get_status_counts(self) -> StatusCounts:
        """Return empty status counts."""
        return StatusCounts()

    def get_domain_distribution(self, limit: int = 10) -> list[DomainDistribution]:
        """Return empty domain distribution."""
        return []

    def get_failure_type_distribution(
        self, limit: int = 10
    ) -> list[FailureTypeDistribution]:
        """Return empty failure type distribution."""
        return []

    def get_recent_activity(self, hours: int = 24, days: int = 7) -> RecentActivity:
        """Return empty recent activity."""
        return RecentActivity()

    def get_resolution_rate(self, days: int = 30) -> float:
        """Return zero resolution rate."""
        return 0.0

    def get_avg_retry_count(self) -> float:
        """Return zero average retry count."""
        return 0.0

    # =========================================================================
    # DLQ List Operations (Paginated)
    # =========================================================================

    def list_entries(
        self,
        page: int = 1,
        page_size: int = 20,
        status: str | None = None,
        domain: str | None = None,
        failure_type: str | None = None,
        order_by: str = "-created_at",
    ) -> PaginatedResult:
        """Return empty paginated result."""
        return PaginatedResult(
            items=[],
            total=0,
            page=page,
            page_size=page_size,
            has_next=False,
            has_prev=False,
        )

    def get_entry_detail(self, entry_id: str) -> dict[str, Any] | None:
        """Return None for entry detail."""
        return None

    # =========================================================================
    # SLA Monitoring
    # =========================================================================

    def get_sla_breaches(
        self,
        sla_threshold_hours: int = 4,
        statuses: list[str] | None = None,
    ) -> dict[str, int]:
        """Return empty SLA breaches (no-op)."""
        logger.debug("null_statistics_repository.called_no_op")
        return {}

    # =========================================================================
    # Cleanup Operations
    # =========================================================================

    def get_cleanup_stats(self) -> CleanupStats:
        """Return empty cleanup stats."""
        return CleanupStats()

    def archive_old_entries(self, older_than_days: int = 30) -> int:
        """Return zero archived entries (no-op)."""
        logger.debug("null_statistics_repository.called_no_op")
        return 0

    def purge_archived(
        self,
        ids: list[str] | None = None,
        older_than_days: int | None = None,
    ) -> int:
        """Return zero purged entries (no-op)."""
        logger.debug("null_statistics_repository.called_no_op")
        return 0

    # =========================================================================
    # Circuit Breaker Statistics
    # =========================================================================

    def get_circuit_breaker_summary(self) -> CircuitBreakerSummary:
        """Return empty circuit breaker summary."""
        return CircuitBreakerSummary()

    def list_circuit_breakers(self) -> list[CircuitBreakerInfo]:
        """Return empty circuit breaker list."""
        return []

    # =========================================================================
    # Persistence (no-op for null adapter)
    # =========================================================================

    def persist_entry(self, entry_data: dict[str, Any]) -> str | None:
        """No-op persist (returns None)."""
        logger.debug("null_statistics_repository.called_no_op")
        return None

    def sync_from_runtime(self, entries: list[dict[str, Any]]) -> int:
        """No-op sync (returns 0)."""
        logger.debug("null_statistics_repository.called_no_op")
        return 0

    # =========================================================================
    # Audit Trail Integration (no-op)
    # =========================================================================

    def get_audit_trail_by_entity(
        self,
        entity_id: str,
        entity_type: str = "dlq_entry",
    ) -> EntityAuditTrail:
        """Return empty audit trail."""
        return EntityAuditTrail(
            entity_id=entity_id,
            entity_type=entity_type,
            domain="unknown",
            entries=[],
        )

    def link_audit_entry(
        self,
        entity_id: str,
        entity_type: str,
        action: str,
        actor_id: str | None = None,
        status: str | None = None,
        details: str | None = None,
        audit_record_hash: str | None = None,
    ) -> bool:
        """No-op link (returns False)."""
        logger.debug("null_statistics_repository.called_no_op")
        return False
