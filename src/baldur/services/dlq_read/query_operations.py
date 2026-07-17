"""
DLQ Query Operations Mixin.

Provides methods for querying DLQ entries + cleanup statistics.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from baldur.models.dlq import CleanupStats
from baldur.utils.time import utc_now

if TYPE_CHECKING:
    from baldur.interfaces.repositories import (
        FailedOperationData,
        FailedOperationRepository,
    )
    from baldur.models.dlq import DLQConfig

logger = structlog.get_logger()

__all__ = ["QueryOperationsMixin"]


class QueryOperationsMixin:
    """Mixin providing DLQ query operations."""

    if TYPE_CHECKING:
        # Host contract — the composed service (dlq_read.service.DLQReadService)
        # provides ``config`` / ``repository`` via DLQCaptureService.
        # Typing-only stubs; no runtime definition, so the MRO is unchanged.
        config: DLQConfig

        @property
        def repository(self) -> FailedOperationRepository: ...

    def get_pending_entries(
        self,
        domain: str | None = None,
        failure_type: str | None = None,
        limit: int = 100,
    ) -> list[FailedOperationData]:
        """
        Get pending DLQ entries.

        Args:
            domain: Filter by domain (optional)
            failure_type: Filter by failure type (optional)
            limit: Maximum number of entries to return

        Returns:
            List of pending FailedOperationData entries
        """
        return self.repository.find_by_status(
            status="pending",
            domain=domain,
            failure_type=failure_type,
            limit=limit,
        )

    def get_replayable_entries(
        self,
        domain: str | None = None,
        failure_type: str | None = None,
        limit: int = 100,
    ) -> list[FailedOperationData]:
        """
        Get entries that can be replayed.

        Entries are replayable if:
        - Status is PENDING
        - retry_count < max_retries

        Args:
            domain: Filter by domain (optional)
            failure_type: Filter by failure type (optional)
            limit: Maximum number of entries to return

        Returns:
            List of replayable FailedOperationData entries
        """
        return self.repository.find_replayable(
            max_retries=self.config.max_replay_attempts,
            domain=domain,
            failure_type=failure_type,
            limit=limit,
        )

    def get_sla_breached_entries(self) -> list[FailedOperationData]:
        """
        Get entries that have breached their SLA.

        SLA thresholds are loaded from configuration (domain-free).
        Uses SLAConfig.get_all_thresholds() to support any configured domain.

        Returns:
            List of SLA-breached FailedOperationData entries
        """
        from baldur.settings.layered_provider import get_layered_settings
        from baldur.settings.sla import SLASettings

        current_time = utc_now()
        # Layered read so a console edit of the sla domain (default_hours) takes
        # effect (686 D1/D5); env base is used when no manager is registered.
        sla_config = get_layered_settings(SLASettings, "sla")

        # Domain-free: dynamically load thresholds for every configured domain.
        return self.repository.find_sla_breached(
            current_time=current_time,
            sla_thresholds=sla_config.get_all_thresholds(),
        )

    def get_expired_entries(self) -> list[FailedOperationData]:
        """
        Get entries that have passed their retention period.

        Returns:
            List of expired FailedOperationData entries
        """
        current_time = utc_now()
        return self.repository.find_expired(current_time=current_time)

    def get_entry_by_id(self, dlq_id: str) -> FailedOperationData | None:
        """
        Get a single DLQ entry by ID.

        Args:
            dlq_id: The DLQ entry ID

        Returns:
            FailedOperationData or None
        """
        return self.repository.get_by_id(dlq_id)

    def get_stats(self) -> dict[str, Any]:
        """
        Get DLQ statistics.

        Returns:
            Dictionary with DLQ statistics
        """
        return self.repository.get_statistics()

    def get_facet_counts(
        self,
        *,
        status: str | None = None,
        domain: str | None = None,
    ) -> dict[str, dict[str, int]]:
        """
        Get faceted status×domain DLQ counts for the admin-console filter.

        Thin pass-through to the repository. The ``by_status`` map is scoped
        by ``domain`` and ``by_domain`` is scoped by ``status`` (standard
        faceted-search semantics); zero-count buckets are dropped. See
        ``FailedOperationRepository.get_facet_counts`` for the full contract.

        Args:
            status: Filter by status (scopes the ``by_domain`` map).
            domain: Filter by domain (scopes the ``by_status`` map).

        Returns:
            Dict with ``by_status`` and ``by_domain`` count maps.
        """
        return self.repository.get_facet_counts(status=status, domain=domain)

    def get_cleanup_stats(self) -> CleanupStats:
        """Cleanup statistics as a CleanupStats value object.

        Bridges the repository's dict-shaped cleanup stats to the canonical
        CleanupStats model (its can_archive/can_purge derive from the
        age-bucketed counts), matching the StatisticsProvider adapters that
        already return CleanupStats. Read by the admin handler
        ``dlq_cleanup_stats`` via attribute access (the OSS read panel body).
        """
        stats = self.repository.get_cleanup_stats()
        return CleanupStats(
            total=stats.get("total", 0),
            by_status=stats.get("by_status", {}),
            resolved_older_than_30_days=stats.get("resolved_older_than_30_days", 0),
            archived_older_than_90_days=stats.get("archived_older_than_90_days", 0),
        )
