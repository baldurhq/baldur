"""
Statistics Repository Interface for Baldur System.

This module defines the interface for statistics/dashboard operations.
Unlike runtime repositories (Redis-based), this interface is designed for
complex aggregate queries that are best handled by SQL/ORM.

Design Principles:
1. Separated from runtime repositories
2. Read-heavy, complex aggregations
3. Graceful degradation via NullStatisticsRepository
4. Framework-agnostic (Django ORM, SQLAlchemy, etc.)
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

# =============================================================================
# Data Transfer Objects
# =============================================================================


@dataclass
class StatusCounts:
    """Status-wise count of DLQ entries."""

    total: int = 0
    pending: int = 0
    resolved: int = 0
    failed: int = 0
    archived: int = 0
    reviewing: int = 0
    replayed: int = 0
    requires_review: int = 0
    rejected: int = 0
    expired: int = 0


@dataclass
class DomainDistribution:
    """Domain-wise distribution of DLQ entries."""

    domain: str
    count: int
    percentage: float


@dataclass
class FailureTypeDistribution:
    """Failure type distribution."""

    failure_type: str
    count: int
    percentage: float


@dataclass
class RecentActivity:
    """Recent activity statistics."""

    new_in_24h: int = 0
    resolved_in_24h: int = 0
    new_in_7d: int = 0
    resolved_in_7d: int = 0
    trend: str = "stable"  # up, down, stable


# CleanupStats canonical home is baldur.models.dlq (519 PR 3 / D-d2).
from baldur.models.dlq import CleanupStats  # noqa: E402, F401


@dataclass
class PaginatedResult:
    """Paginated query result."""

    items: list[Any] = field(default_factory=list)
    total: int = 0
    page: int = 1
    page_size: int = 20
    has_next: bool = False
    has_prev: bool = False

    @property
    def total_pages(self) -> int:
        """Calculate total number of pages."""
        if self.page_size <= 0:
            return 0
        return (self.total + self.page_size - 1) // self.page_size


@dataclass
class CircuitBreakerSummary:
    """Summary of all circuit breakers."""

    total: int = 0
    closed: int = 0
    open: int = 0
    half_open: int = 0


@dataclass
class CircuitBreakerInfo:
    """Information about a single circuit breaker."""

    service_name: str
    state: str  # closed, open, half_open
    failure_count: int = 0
    success_count: int = 0
    last_failure_time: datetime | None = None
    last_state_change: datetime | None = None


@dataclass
class AuditTrailEntry:
    """Single audit trail entry for an entity."""

    timestamp: datetime
    action: str  # store, replay, resolve, reject, archive
    actor_id: str | None = None
    status: str | None = None
    details: str | None = None
    hash_chain: str | None = None  # For tamper-evidence
    previous_hash: str | None = None


@dataclass
class EntityAuditTrail:
    """
    Complete audit trail for a DLQ entity.

    Provides end-to-end traceability from creation to resolution.
    """

    entity_id: str
    entity_type: str  # e.g., "dlq_entry"
    domain: str
    created_at: datetime | None = None
    resolved_at: datetime | None = None
    current_status: str = "unknown"
    entries: list[AuditTrailEntry] = field(default_factory=list)

    @property
    def total_entries(self) -> int:
        """Total number of audit entries."""
        return len(self.entries)

    @property
    def is_chain_valid(self) -> bool:
        """
        Verify hash chain integrity.

        Returns True if all entries have valid hash chain.
        """
        if not self.entries:
            return True

        for i, entry in enumerate(self.entries):
            if i == 0:
                # First entry should have no previous hash
                if entry.previous_hash is not None:
                    return False
            else:
                # Subsequent entries should reference previous hash
                prev_entry = self.entries[i - 1]
                if entry.previous_hash != prev_entry.hash_chain:
                    return False

        return True


# =============================================================================
# Statistics Repository Interface
# =============================================================================


class StatisticsRepositoryInterface(ABC):
    """
    Statistics/Dashboard Repository Interface.

    This interface defines operations for dashboards and analytics.
    Unlike runtime repositories (optimized for speed), this interface
    is designed for complex aggregate queries.

    Implementations:
    - DjangoStatisticsAdapter: Uses Django ORM
    - SQLAlchemyStatisticsAdapter: Uses SQLAlchemy
    - NullStatisticsRepository: Returns empty results (default)

    Usage:
        from baldur.factory import ProviderRegistry

        stats_repo = ProviderRegistry.get_statistics_repo()
        counts = stats_repo.get_status_counts()
    """

    # =========================================================================
    # DLQ Statistics
    # =========================================================================

    @abstractmethod
    def get_status_counts(self) -> StatusCounts:
        """
        Get count of DLQ entries by status.

        Returns:
            StatusCounts with counts for each status
        """
        pass

    @abstractmethod
    def get_domain_distribution(self, limit: int = 10) -> list[DomainDistribution]:
        """
        Get distribution of DLQ entries by domain.

        Args:
            limit: Maximum number of domains to return (top N)

        Returns:
            List of DomainDistribution sorted by count descending
        """
        pass

    @abstractmethod
    def get_failure_type_distribution(
        self, limit: int = 10
    ) -> list[FailureTypeDistribution]:
        """
        Get distribution of DLQ entries by failure type.

        Args:
            limit: Maximum number of failure types to return (top N)

        Returns:
            List of FailureTypeDistribution sorted by count descending
        """
        pass

    @abstractmethod
    def get_recent_activity(self, hours: int = 24, days: int = 7) -> RecentActivity:
        """
        Get recent activity statistics.

        Args:
            hours: Hours to look back for hourly stats
            days: Days to look back for daily stats

        Returns:
            RecentActivity with new/resolved counts and trend
        """
        pass

    @abstractmethod
    def get_resolution_rate(self, days: int = 30) -> float:
        """
        Calculate resolution success rate.

        Args:
            days: Number of days to look back

        Returns:
            Resolution rate as a float (0.0 to 1.0)
        """
        pass

    @abstractmethod
    def get_avg_retry_count(self) -> float:
        """
        Get average retry count across all DLQ entries.

        Returns:
            Average retry count as a float
        """
        pass

    # =========================================================================
    # DLQ List Operations (Paginated)
    # =========================================================================

    @abstractmethod
    def list_entries(
        self,
        page: int = 1,
        page_size: int = 20,
        status: str | None = None,
        domain: str | None = None,
        failure_type: str | None = None,
        order_by: str = "-created_at",
    ) -> PaginatedResult:
        """
        List DLQ entries with pagination and filtering.

        Args:
            page: Page number (1-indexed)
            page_size: Number of items per page
            status: Filter by status (optional)
            domain: Filter by domain (optional)
            failure_type: Filter by failure type (optional)
            order_by: Sort order (prefix with - for descending)

        Returns:
            PaginatedResult containing DLQ entries
        """
        pass

    @abstractmethod
    def get_entry_detail(self, entry_id: str) -> dict[str, Any] | None:
        """
        Get detailed information about a specific DLQ entry.

        Args:
            entry_id: Unique identifier of the entry

        Returns:
            Dict with entry details or None if not found
        """
        pass

    # =========================================================================
    # SLA Monitoring
    # =========================================================================

    @abstractmethod
    def get_sla_breaches(
        self,
        sla_threshold_hours: int = 4,
        statuses: list[str] | None = None,
    ) -> dict[str, int]:
        """
        Get count of SLA breaches by domain.

        Finds DLQ entries that have exceeded the SLA threshold for resolution.

        Args:
            sla_threshold_hours: SLA threshold in hours (default: 4)
            statuses: List of statuses to check (default: pending, reviewing, requires_review)

        Returns:
            Dictionary mapping domain to breach count
        """
        pass

    # =========================================================================
    # Cleanup Operations
    # =========================================================================

    @abstractmethod
    def get_cleanup_stats(self) -> CleanupStats:
        """
        Get statistics for cleanup operations.

        Returns:
            CleanupStats with counts of entries eligible for cleanup
        """
        pass

    @abstractmethod
    def archive_old_entries(self, older_than_days: int = 30) -> int:
        """
        Archive old resolved entries.

        Args:
            older_than_days: Archive entries resolved more than N days ago

        Returns:
            Number of entries archived
        """
        pass

    @abstractmethod
    def purge_archived(
        self,
        ids: list[str] | None = None,
        older_than_days: int | None = None,
    ) -> int:
        """
        Permanently delete archived entries.

        Args:
            ids: Specific entry IDs to purge (optional)
            older_than_days: Purge archived entries older than N days (optional)

        Returns:
            Number of entries purged
        """
        pass

    # =========================================================================
    # Circuit Breaker Statistics
    # =========================================================================

    @abstractmethod
    def get_circuit_breaker_summary(self) -> CircuitBreakerSummary:
        """
        Get summary of all circuit breakers.

        Returns:
            CircuitBreakerSummary with counts by state
        """
        pass

    @abstractmethod
    def list_circuit_breakers(self) -> list[CircuitBreakerInfo]:
        """
        List all circuit breakers with their current state.

        Returns:
            List of CircuitBreakerInfo for all registered circuit breakers
        """
        pass

    # =========================================================================
    # Persistence (for hybrid storage)
    # =========================================================================

    @abstractmethod
    def persist_entry(self, entry_data: dict[str, Any]) -> str | None:
        """
        Persist a DLQ entry to the statistics store.

        Called by runtime layer to sync data to ORM for statistics.
        Can be called asynchronously.

        Args:
            entry_data: Entry data from runtime repository

        Returns:
            Entry ID if persisted, None otherwise
        """
        pass

    @abstractmethod
    def sync_from_runtime(self, entries: list[dict[str, Any]]) -> int:
        """
        Bulk sync entries from runtime repository.

        Used for periodic synchronization.

        Args:
            entries: List of entry data from runtime repository

        Returns:
            Number of entries synced
        """
        pass

    # =========================================================================
    # Audit Trail Integration (The Master Trail)
    # =========================================================================

    @abstractmethod
    def get_audit_trail_by_entity(
        self,
        entity_id: str,
        entity_type: str = "dlq_entry",
    ) -> EntityAuditTrail:
        """
        Get complete audit trail for a specific entity.

        This method provides end-to-end traceability showing all actions
        from creation to resolution with hash chain verification.

        Args:
            entity_id: Unique identifier of the entity (e.g., DLQ ID)
            entity_type: Type of entity (default: "dlq_entry")

        Returns:
            EntityAuditTrail with all audit entries and chain verification

        Example:
            trail = stats_repo.get_audit_trail_by_entity("dlq-123")
            print(f"Total actions: {trail.total_entries}")
            print(f"Chain valid: {trail.is_chain_valid}")
            for entry in trail.entries:
                print(f"{entry.timestamp}: {entry.action} by {entry.actor_id}")
        """
        pass

    @abstractmethod
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
        """
        Link an audit record to an entity.

        Called when audit events are recorded to maintain the relationship
        between DLQ entries and their audit trail.

        Args:
            entity_id: Entity identifier
            entity_type: Entity type
            action: Action performed (store, replay, resolve, etc.)
            actor_id: Who performed the action
            status: New status after the action
            details: Additional details
            audit_record_hash: Hash from the audit system

        Returns:
            True if linked successfully
        """
        pass

    # =========================================================================
    # Async Persistence Support
    # =========================================================================

    def should_persist_async(self) -> bool:
        """
        Determine if persistence should be done asynchronously.

        Override in implementations to enable async persistence.
        Default returns False for synchronous persistence.

        Returns:
            True if async persistence is preferred
        """
        return False

    def get_async_persist_task_name(self) -> str | None:
        """
        Get the Celery task name for async persistence.

        Returns:
            Task name string or None if sync persistence
        """
        return None
