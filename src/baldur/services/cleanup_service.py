"""
Cleanup Service

Handles cleanup and archival operations for DLQ, Config, and Approvals.

Thin Task, Fat Service principle:
- Tasks act only as thin delegators
- All business logic lives in this service

Audit:
- archive_old_dlq_entries: log_system_control_audit(action="archive_dlq")
- cleanup_expired_config: log_system_control_audit(action="cleanup_expired_config")
- purge_archived_dlq_entries: log_system_control_audit(action="purge_dlq_permanent|purge_dlq_dry_run")
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import structlog

from baldur.audit.helpers import log_system_control_audit
from baldur.core.serializable import SerializableMixin

logger = structlog.get_logger()


@dataclass
class CleanupResult(SerializableMixin):
    """Cleanup operation result."""

    success: bool
    operation: str
    count: int = 0
    error: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        result = {
            "success": self.success,
            "operation": self.operation,
            f"{self.operation}_count": self.count,
        }
        if self.error:
            result["error"] = self.error
        if self.details:
            result.update(self.details)
        return result


class CleanupService:
    """
    Service that handles cleanup and archive operations.

    Owns the business logic of every cleanup_tasks.py task.

    Operations:
    - archive_old_dlq_entries: Archive resolved DLQ entries older than 30 days
    - cleanup_expired_config: Clean up expired Pending Config entries
    - expire_approval_requests: Expire approval requests pending more than 72 hours
    - purge_archived_dlq_entries: Permanently delete archived entries older than 90 days
    """

    def archive_old_dlq_entries(
        self,
        older_than_days: int | None = None,
    ) -> CleanupResult:
        """
        Archive resolved DLQ entries older than 30 days.

        Args:
            older_than_days: Threshold in days (None loads from Settings)

        Returns:
            CleanupResult with archived count
        """
        if older_than_days is None:
            from baldur.settings.cleanup import get_cleanup_settings

            older_than_days = get_cleanup_settings().archive_older_than_days

        logger.info(
            "cleanup_service.archiving_dlq_entries_older",
            older_than_days=older_than_days,
        )

        try:
            from baldur.factory.registry import ProviderRegistry

            dlq_service = ProviderRegistry.dlq_service.safe_get()
            if dlq_service is None:
                raise ImportError("baldur_pro DLQService not registered")
            count = dlq_service.archive_old_entries(older_than_days=older_than_days)

            logger.info(
                "cleanup_service.archived_dlq_entries",
                dlq_archived_count=count,
            )

            # === Audit record: DLQ archive ===
            log_system_control_audit(
                action="archive_dlq",
                actor="system",
                old_state={"archived_count": 0},
                new_state={"archived_count": count},
                reason=f"Archive DLQ entries older than {older_than_days} days",
            )

            return CleanupResult(
                success=True,
                operation="archived",
                count=count,
                details={"older_than_days": older_than_days},
            )

        except Exception as e:
            logger.exception(
                "cleanup_service.archive_failed",
                error=e,
            )
            return CleanupResult(
                success=False,
                operation="archived",
                error=str(e),
                details={"older_than_days": older_than_days},
            )

    def cleanup_expired_config(
        self,
        older_than_hours: int | None = None,
    ) -> CleanupResult:
        """
        Clean up expired Pending Config entries.

        Args:
            older_than_hours: Threshold in hours (None loads from Settings)

        Returns:
            CleanupResult with expired count
        """
        if older_than_hours is None:
            from baldur.settings.cleanup import get_cleanup_settings

            older_than_hours = get_cleanup_settings().expired_config_hours

        logger.info(
            "cleanup_service.cleaning_up_configs_older",
            older_than_hours=older_than_hours,
        )

        try:
            from baldur.services.pending_config import get_pending_config_service

            pending_service = get_pending_config_service()
            count = pending_service.cleanup_expired(max_age_hours=older_than_hours)

            logger.info(
                "cleanup_service.cleaned_up_expired_configs",
                expired_cleaned_count=count,
            )

            # === Audit record: expired Pending Config cleanup ===
            log_system_control_audit(
                action="cleanup_expired_config",
                actor="system",
                old_state={"expired_count": 0},
                new_state={"expired_count": count},
                reason=f"Cleanup expired pending configs older than {older_than_hours} hours",
            )

            return CleanupResult(
                success=True,
                operation="expired",
                count=count,
                details={"older_than_hours": older_than_hours},
            )

        except Exception as e:
            logger.exception(
                "cleanup_service.cleanup_failed",
                error=e,
            )
            return CleanupResult(
                success=False,
                operation="expired",
                error=str(e),
                details={"older_than_hours": older_than_hours},
            )

    def expire_approval_requests(
        self,
        older_than_hours: int | None = None,
    ) -> CleanupResult:
        """
        Expire approval requests pending for more than 72 hours.

        Args:
            older_than_hours: Threshold in hours (None loads from Settings)

        Returns:
            CleanupResult with expired count
        """
        if older_than_hours is None:
            from baldur.settings.cleanup import get_cleanup_settings

            older_than_hours = get_cleanup_settings().approval_expiry_hours

        logger.info(
            "cleanup_service.expiring_approval_requests_older",
            older_than_hours=older_than_hours,
        )

        try:
            from baldur.factory.registry import ProviderRegistry

            manager = ProviderRegistry.runtime_config_manager.safe_get()
            if manager is None:
                raise ImportError("baldur_pro RuntimeConfigManager not registered")
            count = manager.expire_old_requests()

            logger.info(
                "cleanup_service.expired_approval_requests",
                expired_requests_count=count,
            )

            return CleanupResult(
                success=True,
                operation="expired",
                count=count,
                details={"older_than_hours": older_than_hours},
            )

        except Exception as e:
            logger.exception(
                "cleanup_service.approval_expiration_failed",
                error=e,
            )
            return CleanupResult(
                success=False,
                operation="expired",
                error=str(e),
                details={"older_than_hours": older_than_hours},
            )

    def cleanup_stale_cb_keys(
        self,
        retention_days: int | None = None,
    ) -> CleanupResult:
        """Delete orphan ``cb:{service_name}`` Redis hashes past retention.

        Removes stale CB state for renamed/decommissioned services so that
        long-lived deployments do not accumulate indefinite key churn.
        Repo SCAN guards (max_iterations + 2s deadline) make partial cleanup
        acceptable — the next daily run continues where this one stopped.

        Args:
            retention_days: Cleanup threshold in days (defaults to
                ``CleanupSettings.cb_stale_key_retention_days``).

        Returns:
            CleanupResult with deleted count.
        """
        if retention_days is None:
            from baldur.settings.cleanup import get_cleanup_settings

            retention_days = get_cleanup_settings().cb_stale_key_retention_days

        logger.info(
            "cleanup_service.cleaning_up_stale_cb_keys",
            retention_days=retention_days,
        )

        try:
            from baldur.factory import ProviderRegistry

            repo = ProviderRegistry.get_circuit_breaker_repo()
            # `cleanup_stale_keys` lives only on the Redis CB impl (TTL-keyed
            # state); memory/sql repos don't accumulate stale keys and so the
            # ABC doesn't declare it. Duck-type: degrade to 0 in OSS.
            cleanup_fn = getattr(repo, "cleanup_stale_keys", None)
            count = cleanup_fn(retention_days=retention_days) if cleanup_fn else 0

            logger.info(
                "cleanup_service.cleaned_up_stale_cb_keys",
                cb_stale_keys_deleted=count,
            )

            log_system_control_audit(
                action="cleanup_stale_cb_keys",
                actor="system",
                old_state={"deleted_count": 0},
                new_state={"deleted_count": count},
                reason=f"Cleanup CB state entries older than {retention_days} days",
            )

            return CleanupResult(
                success=True,
                operation="deleted",
                count=count,
                details={"retention_days": retention_days},
            )

        except Exception as e:
            logger.exception(
                "cleanup_service.cleanup_stale_cb_keys_failed",
                error=e,
            )
            return CleanupResult(
                success=False,
                operation="deleted",
                error=str(e),
                details={"retention_days": retention_days},
            )

    def cleanup_memory_cache_expired(self) -> CleanupResult:
        """Drive periodic expiration cleanup across all live InMemoryCacheAdapter instances.

        Iterates the class-level ``InMemoryCacheAdapter._instances`` WeakSet
        and invokes the public, lock-acquiring ``cleanup_expired()`` method on
        each. Lazy on-access expiration only removes accessed keys; this task
        sweeps cold expired keys that would otherwise persist for the lifetime
        of the process.

        Returns:
            CleanupResult with total expired entries removed across all instances.
        """
        try:
            from baldur.adapters.cache.memory_adapter import InMemoryCacheAdapter

            instances = list(InMemoryCacheAdapter._instances)
            instance_count = len(instances)
            removed_total = 0
            for instance in instances:
                try:
                    removed_total += instance.cleanup_expired()
                except Exception as e:
                    logger.warning(
                        "cleanup_service.memory_cache_instance_cleanup_failed",
                        error=e,
                    )

            logger.info(
                "cleanup_service.cleaned_up_memory_cache",
                removed_count=removed_total,
                instance_count=instance_count,
                live_instances=len(InMemoryCacheAdapter._instances),
            )

            return CleanupResult(
                success=True,
                operation="removed",
                count=removed_total,
                details={"instance_count": instance_count},
            )

        except Exception as e:
            logger.exception(
                "cleanup_service.cleanup_memory_cache_expired_failed",
                error=e,
            )
            return CleanupResult(
                success=False,
                operation="removed",
                error=str(e),
            )

    def purge_archived_dlq_entries(
        self,
        older_than_days: int | None = None,
        dry_run: bool = False,
    ) -> CleanupResult:
        """
        Permanently delete archive entries older than 90 days.

        HIGH RISK: Requires pre-approval, irreversible.

        Args:
            older_than_days: Deletion threshold in days (None loads from Settings)
            dry_run: If True, returns the affected count without deleting

        Returns:
            CleanupResult with purged count
        """
        if older_than_days is None:
            from baldur.settings.cleanup import get_cleanup_settings

            older_than_days = get_cleanup_settings().purge_older_than_days

        logger.warning(
            "cleanup_service.purging_archived_dlq_entries",
            older_than_days=older_than_days,
            dry_run=dry_run,
        )

        try:
            from baldur.factory.registry import ProviderRegistry

            dlq_service = ProviderRegistry.dlq_service.safe_get()
            if dlq_service is None:
                raise ImportError("baldur_pro DLQService not registered")

            if dry_run:
                # dry_run mode: only count the affected entries
                count = dlq_service.count_archived_older_than(
                    older_than_days=older_than_days
                )
                logger.info(
                    "cleanup_service.dry_run_purge_archived",
                    dry_run_purge_count=count,
                )

                # === Audit record: DRY RUN mode (no actual deletion) ===
                log_system_control_audit(
                    action="purge_dlq_dry_run",
                    actor="system",
                    old_state={"purged_count": 0},
                    new_state={"would_purge_count": count, "dry_run": True},
                    reason=f"DRY RUN: Would purge {count} archived entries older than {older_than_days} days",
                )
            else:
                count = dlq_service.purge_archived(older_than_days=older_than_days)
                logger.warning(
                    "cleanup_service.permanently_deleted_entries",
                    dlq_purged_count=count,
                )

                # === Audit record: permanent deletion (high risk, irreversible) ===
                log_system_control_audit(
                    action="purge_dlq_permanent",
                    actor="system",
                    old_state={"purged_count": 0},
                    new_state={
                        "purged_count": count,
                        "permanent": True,
                        "unrecoverable": True,
                    },
                    reason=f"PERMANENT DELETION: Purged {count} archived entries older than {older_than_days} days - UNRECOVERABLE",
                )

            return CleanupResult(
                success=True,
                operation="purged",
                count=count,
                details={
                    "older_than_days": older_than_days,
                    "dry_run": dry_run,
                    "warning": (
                        "PERMANENT DELETION - UNRECOVERABLE"
                        if not dry_run
                        else "DRY RUN - No actual deletion"
                    ),
                },
            )

        except Exception as e:
            logger.exception(
                "cleanup_service.purge_failed",
                error=e,
            )
            return CleanupResult(
                success=False,
                operation="purged",
                error=str(e),
                details={"older_than_days": older_than_days, "dry_run": dry_run},
            )


# =============================================================================
# Singleton
# =============================================================================

from baldur.utils.singleton import make_singleton_factory

get_cleanup_service, configure_cleanup_service, reset_cleanup_service = (
    make_singleton_factory("cleanup_service", CleanupService)
)

__all__ = [
    "CleanupResult",
    "CleanupService",
    "get_cleanup_service",
    "configure_cleanup_service",
    "reset_cleanup_service",
]
