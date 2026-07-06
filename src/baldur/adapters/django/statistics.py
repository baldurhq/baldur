"""
Django Statistics Adapter for Baldur System.

This adapter implements StatisticsRepositoryInterface using Django ORM.
It is designed to be domain-free - the actual models are provided by
the application, not by baldur package.

Usage:
    # In your Django app's apps.py
    from baldur.factory import ProviderRegistry
    from baldur.adapters.django.statistics import DjangoStatisticsAdapter

    class MyAppConfig(AppConfig):
        def ready(self):
            from baldur.adapters.django.models import FailedOperation

            ProviderRegistry.register_statistics_adapter(
                DjangoStatisticsAdapter(
                    failed_operation_model=FailedOperation,
                )
            )
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Any

import structlog

from baldur.interfaces.statistics import (
    AuditTrailEntry,
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

if TYPE_CHECKING:
    from django.db.models import Model

    from baldur.adapters.django.models._abstract_failed_operation import (
        AbstractFailedOperation,
    )

logger = structlog.get_logger()


class DjangoStatisticsAdapter(StatisticsRepositoryInterface):
    """
    Django ORM implementation of StatisticsRepositoryInterface.

    This adapter is domain-free - models are provided by the application.
    It uses Django's powerful ORM for complex aggregate queries.

    Attributes:
        failed_operation_model: Django model for DLQ entries
        circuit_breaker_model: Django model for CB state (optional)
    """

    def __init__(
        self,
        failed_operation_model: type[AbstractFailedOperation] | None = None,
        circuit_breaker_model: type[Model] | None = None,
    ):
        """
        Initialize Django Statistics Adapter.

        Args:
            failed_operation_model: Concrete model inheriting from
                AbstractFailedOperation (manager + fields visible to django-stubs).
            circuit_breaker_model: User-provided model class for CB state. No
                abstract base is shipped — caller's choice of concrete model.
        """
        self._failed_operation_model: type[AbstractFailedOperation] | None = (
            failed_operation_model
        )
        self._circuit_breaker_model: type[Model] | None = circuit_breaker_model

        if failed_operation_model:
            logger.info(
                "django_statistics_adapter.initialized_model",
                failed_operation_model=failed_operation_model.__name__,
            )
        else:
            logger.warning("django_statistics_adapter.model_not_provided")

    def _get_model(self) -> type[AbstractFailedOperation]:
        """Get the FailedOperation model. Callers must call _check_model() first."""
        assert self._failed_operation_model is not None, (
            "_get_model() requires _check_model() to have returned True first"
        )
        return self._failed_operation_model

    def _check_model(self) -> bool:
        """Check if model is available."""
        if self._failed_operation_model is None:
            logger.warning("django_statistics_adapter.model_configured")
            return False
        return True

    # =========================================================================
    # DLQ Statistics
    # =========================================================================

    def get_status_counts(self) -> StatusCounts:
        """Get count of DLQ entries by status using Django aggregation."""
        if not self._check_model():
            return StatusCounts()

        try:
            from django.db.models import Count

            model = self._get_model()
            queryset = model.objects.values("status").annotate(count=Count("id"))

            counts = StatusCounts()
            for row in queryset:
                status = row["status"]
                count = row["count"]
                counts.total += count

                if hasattr(counts, status):
                    setattr(counts, status, count)

            return counts
        except Exception as e:
            logger.exception(
                "django_statistics_adapter.error",
                error=e,
            )
            return StatusCounts()

    def get_domain_distribution(self, limit: int = 10) -> list[DomainDistribution]:
        """Get distribution of DLQ entries by domain."""
        if not self._check_model():
            return []

        try:
            from django.db.models import Count

            model = self._get_model()
            total = model.objects.count()

            if total == 0:
                return []

            queryset = (
                model.objects.values("domain")
                .annotate(count=Count("id"))
                .order_by("-count")[:limit]
            )

            return [
                DomainDistribution(
                    domain=row["domain"] or "unknown",
                    count=row["count"],
                    percentage=round(row["count"] / total * 100, 2),
                )
                for row in queryset
            ]
        except Exception as e:
            logger.exception(
                "django_statistics_adapter.error",
                error=e,
            )
            return []

    def get_failure_type_distribution(
        self, limit: int = 10
    ) -> list[FailureTypeDistribution]:
        """Get distribution of DLQ entries by failure type."""
        if not self._check_model():
            return []

        try:
            from django.db.models import Count

            model = self._get_model()
            total = model.objects.count()

            if total == 0:
                return []

            queryset = (
                model.objects.values("failure_type")
                .annotate(count=Count("id"))
                .order_by("-count")[:limit]
            )

            return [
                FailureTypeDistribution(
                    failure_type=row["failure_type"] or "unknown",
                    count=row["count"],
                    percentage=round(row["count"] / total * 100, 2),
                )
                for row in queryset
            ]
        except Exception as e:
            logger.exception(
                "django_statistics_adapter.error",
                error=e,
            )
            return []

    def get_recent_activity(self, hours: int = 24, days: int = 7) -> RecentActivity:
        """Get recent activity statistics."""
        if not self._check_model():
            return RecentActivity()

        try:
            from django.utils import timezone

            model = self._get_model()
            now = timezone.now()
            hours_ago = now - timedelta(hours=hours)
            days_ago = now - timedelta(days=days)

            # New entries
            new_in_24h = model.objects.filter(created_at__gte=hours_ago).count()
            new_in_7d = model.objects.filter(created_at__gte=days_ago).count()

            # Resolved entries
            resolved_in_24h = model.objects.filter(resolved_at__gte=hours_ago).count()
            resolved_in_7d = model.objects.filter(resolved_at__gte=days_ago).count()

            # Calculate trend
            prev_week = model.objects.filter(
                created_at__gte=days_ago - timedelta(days=7),
                created_at__lt=days_ago,
            ).count()

            if new_in_7d > prev_week * 1.1:
                trend = "up"
            elif new_in_7d < prev_week * 0.9:
                trend = "down"
            else:
                trend = "stable"

            return RecentActivity(
                new_in_24h=new_in_24h,
                resolved_in_24h=resolved_in_24h,
                new_in_7d=new_in_7d,
                resolved_in_7d=resolved_in_7d,
                trend=trend,
            )
        except Exception as e:
            logger.exception(
                "django_statistics_adapter.error",
                error=e,
            )
            return RecentActivity()

    def get_resolution_rate(self, days: int = 30) -> float:
        """Calculate resolution success rate."""
        if not self._check_model():
            return 0.0

        try:
            from django.utils import timezone

            model = self._get_model()
            since = timezone.now() - timedelta(days=days)

            total = model.objects.filter(created_at__gte=since).count()
            if total == 0:
                return 0.0

            resolved = model.objects.filter(
                created_at__gte=since,
                status="resolved",
            ).count()

            return round(resolved / total, 4)
        except Exception as e:
            logger.exception(
                "django_statistics_adapter.error",
                error=e,
            )
            return 0.0

    def get_avg_retry_count(self) -> float:
        """Get average retry count across all DLQ entries."""
        if not self._check_model():
            return 0.0

        try:
            from django.db.models import Avg

            model = self._get_model()
            result = model.objects.aggregate(avg_retry=Avg("retry_count"))
            return round(result["avg_retry"] or 0.0, 2)
        except Exception as e:
            logger.exception(
                "django_statistics_adapter.error",
                error=e,
            )
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
        """List DLQ entries with pagination and filtering."""
        if not self._check_model():
            return PaginatedResult(page=page, page_size=page_size)

        try:
            model = self._get_model()
            queryset = model.objects.all()

            # Apply filters
            if status:
                queryset = queryset.filter(status=status)
            if domain:
                queryset = queryset.filter(domain=domain)
            if failure_type:
                queryset = queryset.filter(failure_type=failure_type)

            # Order
            queryset = queryset.order_by(order_by)

            # Count
            total = queryset.count()

            # Paginate
            offset = (page - 1) * page_size
            items = list(queryset[offset : offset + page_size].values())

            return PaginatedResult(
                items=items,
                total=total,
                page=page,
                page_size=page_size,
                has_next=offset + page_size < total,
                has_prev=page > 1,
            )
        except Exception as e:
            logger.exception(
                "django_statistics_adapter.error",
                error=e,
            )
            return PaginatedResult(page=page, page_size=page_size)

    def get_entry_detail(self, entry_id: str) -> dict[str, Any] | None:
        """Get detailed information about a specific DLQ entry."""
        if not self._check_model():
            return None

        try:
            model = self._get_model()
            entry = model.objects.filter(pk=entry_id).values().first()
            return dict(entry) if entry else None
        except Exception as e:
            logger.exception(
                "django_statistics_adapter.error",
                error=e,
            )
            return None

    # =========================================================================
    # SLA Monitoring
    # =========================================================================

    def get_sla_breaches(
        self,
        sla_threshold_hours: int = 4,
        statuses: list[str] | None = None,
    ) -> dict[str, int]:
        """
        Get count of SLA breaches by domain.

        Finds DLQ entries that have exceeded the SLA threshold for resolution.
        """
        if not self._check_model():
            return {}

        try:
            from django.db.models import Count
            from django.utils import timezone

            model = self._get_model()

            if statuses is None:
                statuses = ["pending", "reviewing", "requires_review"]

            cutoff = timezone.now() - timedelta(hours=sla_threshold_hours)

            # Find entries that have exceeded SLA
            return dict(
                model.objects.filter(
                    status__in=statuses,
                    created_at__lt=cutoff,
                )
                .values("domain")
                .annotate(count=Count("id"))
                .values_list("domain", "count")
            )

        except Exception as e:
            logger.exception(
                "django_statistics_adapter.error",
                error=e,
            )
            return {}

    # =========================================================================
    # Cleanup Operations
    # =========================================================================

    def get_cleanup_stats(self) -> CleanupStats:
        """Get statistics for cleanup operations."""
        if not self._check_model():
            return CleanupStats()

        try:
            from django.db.models import Count
            from django.utils import timezone

            model = self._get_model()

            # Count by status
            status_counts = dict(
                model.objects.values("status")
                .annotate(count=Count("id"))
                .values_list("status", "count")
            )

            # Resolved older than 30 days
            thirty_days_ago = timezone.now() - timedelta(days=30)
            resolved_old = model.objects.filter(
                status="resolved",
                resolved_at__lt=thirty_days_ago,
            ).count()

            # Archived older than 90 days
            ninety_days_ago = timezone.now() - timedelta(days=90)
            archived_old = model.objects.filter(
                status="archived",
                updated_at__lt=ninety_days_ago,
            ).count()

            return CleanupStats(
                total=model.objects.count(),
                by_status=status_counts,
                resolved_older_than_30_days=resolved_old,
                archived_older_than_90_days=archived_old,
            )
        except Exception as e:
            logger.exception(
                "django_statistics_adapter.error",
                error=e,
            )
            return CleanupStats()

    def archive_old_entries(self, older_than_days: int = 30) -> int:
        """Archive old resolved entries."""
        if not self._check_model():
            return 0

        try:
            from django.utils import timezone

            model = self._get_model()
            cutoff = timezone.now() - timedelta(days=older_than_days)

            count = model.objects.filter(
                status="resolved",
                resolved_at__lt=cutoff,
            ).update(status="archived")

            logger.info(
                "django_statistics_adapter.archived_entries",
                archived_count=count,
            )
            return count
        except Exception as e:
            logger.exception(
                "django_statistics_adapter.error",
                error=e,
            )
            return 0

    def purge_archived(
        self,
        ids: list[str] | None = None,
        older_than_days: int | None = None,
    ) -> int:
        """Permanently delete archived entries."""
        if not self._check_model():
            return 0

        try:
            from django.utils import timezone

            model = self._get_model()
            queryset = model.objects.filter(status="archived")

            if ids:
                queryset = queryset.filter(pk__in=ids)

            if older_than_days:
                cutoff = timezone.now() - timedelta(days=older_than_days)
                queryset = queryset.filter(updated_at__lt=cutoff)

            count, _ = queryset.delete()

            logger.info(
                "django_statistics_adapter.purged_entries",
                purged_count=count,
            )
            return count
        except Exception as e:
            logger.exception(
                "django_statistics_adapter.error",
                error=e,
            )
            return 0

    # =========================================================================
    # Circuit Breaker Statistics
    # =========================================================================

    def get_circuit_breaker_summary(self) -> CircuitBreakerSummary:
        """Get summary of all circuit breakers."""
        if not self._circuit_breaker_model:
            # Fallback to Redis-based stats if available
            return self._get_cb_summary_from_redis()

        try:
            from django.db.models import Count

            model = self._circuit_breaker_model
            # No abstract base for CB models is shipped, so .objects is invisible
            # to django-stubs on the generic Model type.
            queryset = model.objects.values("state").annotate(count=Count("id"))  # type: ignore[attr-defined]

            summary = CircuitBreakerSummary()
            for row in queryset:
                state = row["state"]
                count = row["count"]
                summary.total += count

                if state == "closed":
                    summary.closed = count
                elif state == "open":
                    summary.open = count
                elif state == "half_open":
                    summary.half_open = count

            return summary
        except Exception as e:
            logger.exception(
                "django_statistics_adapter.error",
                error=e,
            )
            return CircuitBreakerSummary()

    def _get_cb_summary_from_redis(self) -> CircuitBreakerSummary:
        """Get CB summary from Redis runtime repository."""
        try:
            from baldur.factory import ProviderRegistry

            cb_repo = ProviderRegistry.get_circuit_breaker_repo()
            states = cb_repo.get_all_states()

            summary = CircuitBreakerSummary(total=len(states))
            for state in states:
                if state.state == "closed":
                    summary.closed += 1
                elif state.state == "open":
                    summary.open += 1
                elif state.state == "half_open":
                    summary.half_open += 1

            return summary
        except Exception as e:
            logger.exception(
                "django_statistics_adapter.error",
                error=e,
            )
            return CircuitBreakerSummary()

    def list_circuit_breakers(self) -> list[CircuitBreakerInfo]:
        """List all circuit breakers with their current state."""
        if not self._circuit_breaker_model:
            # Fallback to Redis-based list if available
            return self._list_cbs_from_redis()

        try:
            model = self._circuit_breaker_model
            # No abstract base for CB models is shipped, so .objects is invisible
            # to django-stubs on the generic Model type.
            entries = model.objects.all().values()  # type: ignore[attr-defined]

            return [
                CircuitBreakerInfo(
                    service_name=entry.get("service_name", "unknown"),
                    state=entry.get("state", "closed"),
                    failure_count=entry.get("failure_count", 0),
                    success_count=entry.get("success_count", 0),
                    last_failure_time=entry.get("last_failure_time"),
                    last_state_change=entry.get("last_state_change"),
                )
                for entry in entries
            ]
        except Exception as e:
            logger.exception(
                "django_statistics_adapter.error",
                error=e,
            )
            return []

    def _list_cbs_from_redis(self) -> list[CircuitBreakerInfo]:
        """List CBs from Redis runtime repository."""
        try:
            from baldur.factory import ProviderRegistry

            cb_repo = ProviderRegistry.get_circuit_breaker_repo()
            states = cb_repo.get_all_states()

            return [
                CircuitBreakerInfo(
                    service_name=state.service_name,
                    state=(
                        state.state.value
                        if hasattr(state.state, "value")
                        else str(state.state)
                    ),
                    failure_count=state.failure_count,
                    success_count=state.success_count,
                    last_failure_time=state.last_failure_at,
                    last_state_change=state.opened_at,
                )
                for state in states
            ]
        except Exception as e:
            logger.exception(
                "django_statistics_adapter.error",
                error=e,
            )
            return []

    # =========================================================================
    # Persistence (for hybrid storage)
    # =========================================================================

    def persist_entry(self, entry_data: dict[str, Any]) -> str | None:
        """Persist a DLQ entry to the statistics store."""
        if not self._check_model():
            return None

        try:
            model = self._get_model()

            # Create or update
            entry_id = entry_data.get("id")
            if entry_id:
                obj, created = model.objects.update_or_create(
                    pk=entry_id,
                    defaults=entry_data,
                )
            else:
                obj = model.objects.create(**entry_data)

            return str(obj.pk)
        except Exception as e:
            logger.exception(
                "django_statistics_adapter.error",
                error=e,
            )
            return None

    def sync_from_runtime(self, entries: list[dict[str, Any]]) -> int:
        """Bulk sync entries from runtime repository."""
        if not self._check_model():
            return 0

        synced = 0
        for entry_data in entries:
            if self.persist_entry(entry_data):
                synced += 1

        logger.info(
            "django_statistics_adapter.synced_entries",
            synced=synced,
            entries_count=len(entries),
        )
        return synced

    # =========================================================================
    # Audit Trail Integration (The Master Trail)
    # =========================================================================

    def get_audit_trail_by_entity(
        self,
        entity_id: str,
        entity_type: str = "dlq_entry",
    ) -> EntityAuditTrail:
        """
        Get complete audit trail for a specific entity.

        Retrieves all audit log entries related to a DLQ entry and
        verifies the hash chain integrity.
        """
        # Start with basic entity info from DLQ
        trail = EntityAuditTrail(
            entity_id=entity_id,
            entity_type=entity_type,
            domain="unknown",
            entries=[],
        )

        # Get DLQ entry details if available
        if self._check_model() and entity_type == "dlq_entry":
            try:
                model = self._get_model()
                entry = model.objects.filter(pk=entity_id).first()
                if entry:
                    trail.domain = getattr(entry, "domain", "unknown")
                    trail.created_at = getattr(entry, "created_at", None)
                    trail.resolved_at = getattr(entry, "resolved_at", None)
                    trail.current_status = getattr(entry, "status", "unknown")
            except Exception as e:
                logger.warning(
                    "django_statistics_adapter.get_dlq_entry_failed",
                    error=e,
                )

        # Get audit log entries from the audit adapter
        try:
            from baldur.factory import ProviderRegistry

            audit_adapter = ProviderRegistry.get_audit_adapter()

            # Try to get audit entries by entity reference
            # This depends on how the audit adapter stores entity references
            if hasattr(audit_adapter, "get_entries_by_entity"):
                audit_entries = audit_adapter.get_entries_by_entity(
                    entity_id=entity_id,
                    entity_type=entity_type,
                )

                for audit_entry in audit_entries:
                    trail.entries.append(
                        AuditTrailEntry(
                            timestamp=audit_entry.timestamp,
                            action=(
                                audit_entry.action.value
                                if hasattr(audit_entry.action, "value")
                                else str(audit_entry.action)
                            ),
                            actor_id=audit_entry.actor_id,
                            status=(
                                audit_entry.new_value
                                if hasattr(audit_entry, "new_value")
                                else None
                            ),
                            details=(
                                audit_entry.details
                                if hasattr(audit_entry, "details")
                                else None
                            ),
                            hash_chain=(
                                audit_entry.hash
                                if hasattr(audit_entry, "hash")
                                else None
                            ),
                            previous_hash=(
                                audit_entry.previous_hash
                                if hasattr(audit_entry, "previous_hash")
                                else None
                            ),
                        )
                    )
        except Exception as e:
            logger.debug(
                "django_statistics_adapter.audit_trail_lookup_skipped",
                error=e,
            )

        return trail

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

        Creates a mapping between DLQ entries and their audit records
        for efficient trail retrieval.
        """
        # This could be stored in a separate mapping table or
        # added as metadata to the DLQ entry itself
        if not self._check_model():
            return False

        try:
            model = self._get_model()
            entry = model.objects.filter(pk=entity_id).first()
            if not entry:
                return False

            # If the model has a metadata field, we can store audit references
            if hasattr(entry, "metadata"):
                metadata = entry.metadata or {}
                audit_refs = metadata.get("audit_references", [])
                audit_refs.append(
                    {
                        "action": action,
                        "actor_id": actor_id,
                        "status": status,
                        "hash": audit_record_hash,
                    }
                )
                metadata["audit_references"] = audit_refs
                entry.metadata = metadata
                entry.save(update_fields=["metadata", "updated_at"])

            return True
        except Exception as e:
            logger.exception(
                "django_statistics_adapter.error",
                error=e,
            )
            return False

    # =========================================================================
    # Async Persistence Configuration
    # =========================================================================

    def should_persist_async(self) -> bool:
        """
        Check if async persistence is configured.

        Reads from Django settings:
        BALDUR_ASYNC_PERSISTENCE = True
        """
        try:
            from django.conf import settings

            return getattr(settings, "BALDUR_ASYNC_PERSISTENCE", False)
        except Exception:
            return False

    def get_async_persist_task_name(self) -> str | None:
        """Get the Celery task name for async persistence."""
        if self.should_persist_async():
            return "baldur.adapters.celery.tasks.async_persist_dlq_entry"
        return None
