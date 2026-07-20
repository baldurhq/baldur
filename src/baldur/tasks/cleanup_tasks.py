"""
🧹 Cleanup Lane (Cleanup & Expire) Celery Tasks

Thin Task, Fat Service principle:
- The functions in this file act only as simple delegators
- All business logic is handled in CleanupService

Tasks:
1. archive_old_dlq_entries - archive resolved DLQ entries older than 30 days
2. cleanup_expired_config - clean up expired Pending Config entries
3. expire_approval_requests - expire approval requests pending for more than 72 hours
4. purge_archived_dlq_entries - permanently delete archived entries older than 90 days (high-risk)
"""

from __future__ import annotations

from typing import Any

import structlog

logger = structlog.get_logger()


# =============================================================================
# Thin Task Wrappers
# =============================================================================


def archive_old_dlq_entries(older_than_days: int = 30) -> dict[str, Any]:
    """
    Archive resolved DLQ entries older than 30 days.

    This function is a thin wrapper that delegates to CleanupService.
    All business logic is handled in the service layer.

    Args:
        older_than_days: archive threshold in days (default 30)

    Returns:
        dict: {
            "success": bool,
            "archived_count": int,
            "older_than_days": int,
        }
    """
    from baldur.services.cleanup_service import get_cleanup_service
    from baldur.services.daily_report import record_cleanup_result

    try:
        service = get_cleanup_service()
        result = service.archive_old_dlq_entries(older_than_days=older_than_days)
        result_dict = result.to_dict()
        record_cleanup_result("baldur.archive_old_dlq_entries", result_dict)
        return result_dict

    except Exception as e:
        logger.exception(
            "cleanup_task.failed",
            error=e,
        )
        raise


def cleanup_expired_config(older_than_hours: int = 24) -> dict[str, Any]:
    """
    Clean up expired Pending Config entries.

    This function is a thin wrapper that delegates to CleanupService.

    Args:
        older_than_hours: expiry threshold in hours (default 24)

    Returns:
        dict: {
            "success": bool,
            "expired_count": int,
            "older_than_hours": int,
        }
    """
    from baldur.services.cleanup_service import get_cleanup_service
    from baldur.services.daily_report import record_cleanup_result

    try:
        service = get_cleanup_service()
        result = service.cleanup_expired_config(older_than_hours=older_than_hours)
        result_dict = result.to_dict()
        record_cleanup_result("baldur.cleanup_expired_config", result_dict)
        return result_dict

    except Exception as e:
        logger.exception(
            "cleanup_task.failed",
            error=e,
        )
        raise


def expire_approval_requests(older_than_hours: int = 72) -> dict[str, Any]:
    """
    Expire approval requests pending for more than 72 hours.

    This function is a thin wrapper that delegates to CleanupService.

    Args:
        older_than_hours: expiry threshold in hours (default 72)

    Returns:
        dict: {
            "success": bool,
            "expired_count": int,
            "older_than_hours": int,
        }
    """
    from baldur.services.cleanup_service import get_cleanup_service

    try:
        service = get_cleanup_service()
        result = service.expire_approval_requests(older_than_hours=older_than_hours)
        return result.to_dict()

    except Exception as e:
        logger.exception(
            "cleanup_task.failed",
            error=e,
        )
        raise


def purge_archived_dlq_entries(
    older_than_days: int = 90,
    dry_run: bool = False,
) -> dict[str, Any]:
    """
    Permanently delete archived entries older than 90 days.

    ⚠️ High-risk: prior approval required, irreversible

    This function is a thin wrapper that delegates to CleanupService.

    Args:
        older_than_days: deletion threshold in days (default 90)
        dry_run: if True, do not actually delete and return only the target count

    Returns:
        dict: {
            "success": bool,
            "purged_count": int,
            "older_than_days": int,
            "warning": str,
        }
    """
    from baldur.services.cleanup_service import get_cleanup_service
    from baldur.services.daily_report import record_cleanup_result

    try:
        service = get_cleanup_service()
        result = service.purge_archived_dlq_entries(
            older_than_days=older_than_days,
            dry_run=dry_run,
        )
        result_dict = result.to_dict()
        record_cleanup_result("baldur.purge_archived_dlq_entries", result_dict)
        return result_dict

    except Exception as e:
        logger.exception(
            "cleanup_task.failed",
            error=e,
        )
        raise


def cleanup_stale_cb_keys(retention_days: int | None = None) -> dict[str, Any]:
    """Delete orphan ``cb:{service_name}`` Redis hashes past retention.

    Thin wrapper that delegates to CleanupService. Removes CB state for
    renamed/decommissioned services so long-lived deployments do not
    accumulate indefinite key churn.

    Args:
        retention_days: Cleanup threshold (None loads from settings).

    Returns:
        dict: {"success": bool, "deleted_count": int, "retention_days": int}
    """
    from baldur.services.cleanup_service import get_cleanup_service

    try:
        service = get_cleanup_service()
        result = service.cleanup_stale_cb_keys(retention_days=retention_days)
        return result.to_dict()

    except Exception as e:
        logger.exception(
            "cleanup_task.failed",
            error=e,
        )
        raise


def cleanup_memory_cache_expired() -> dict[str, Any]:
    """Sweep expired entries across all live InMemoryCacheAdapter instances.

    Thin wrapper that delegates to CleanupService. Removes cold expired keys
    that lazy on-access expiration would leave behind in long-running
    test/dev/canary processes.

    Returns:
        dict: {"success": bool, "removed_count": int, "instance_count": int}
    """
    from baldur.services.cleanup_service import get_cleanup_service

    try:
        service = get_cleanup_service()
        result = service.cleanup_memory_cache_expired()
        return result.to_dict()

    except Exception as e:
        logger.exception(
            "cleanup_task.failed",
            error=e,
        )
        raise


def refresh_audit_wal_metrics() -> dict[str, Any]:
    """Refresh audit WAL gauges from current ``WriteAheadLog.get_stats()``.

    Sets ``baldur_wal_total_files`` and ``baldur_wal_current_size_bytes`` so
    SREs can observe disk pressure and file-rotation health. No-op if WAL is
    disabled (``_get_wal()`` returns None) or audit is not installed.

    Returns:
        dict: {"success": bool, "skipped": bool, "total_files": int, "current_size_bytes": int}
    """
    try:
        from baldur_pro.services.audit import _get_wal

        wal = _get_wal()
        if wal is None:
            return {"success": True, "skipped": True}

        stats = wal.get_stats()

        from baldur.metrics.drift_metrics import (
            update_wal_current_size_bytes,
            update_wal_total_files,
        )

        update_wal_total_files(stats.total_files)
        update_wal_current_size_bytes(stats.current_size_bytes)

        return {
            "success": True,
            "skipped": False,
            "total_files": stats.total_files,
            "current_size_bytes": stats.current_size_bytes,
        }

    except Exception as e:
        logger.exception(
            "cleanup_task.failed",
            error=e,
        )
        raise


def flush_expired_jwt_tokens() -> dict[str, Any]:
    """
    Clean up expired JWT OutstandingTokens.

    Runs the flushexpiredtokens management command of rest_framework_simplejwt to
    remove expired OutstandingToken records from the DB.

    Since OutstandingTokens accumulate after the JWT blacklist integration (#217),
    periodic cleanup is required.

    Precondition:
        - rest_framework_simplejwt.token_blacklist is included in INSTALLED_APPS

    Returns:
        dict: {
            "success": bool,
            "message": str,
            "skipped": bool (optional, only when token_blacklist is not installed),
        }
    """
    try:
        from django.apps import apps

        if not apps.is_installed("rest_framework_simplejwt.token_blacklist"):
            msg = "token_blacklist app is not installed, skipping."
            logger.info(
                "cleanup_task.skipped",
                detail_msg=msg,
            )
            return {"success": True, "message": msg, "skipped": True}

        from django.core.management import call_command

        call_command("flushexpiredtokens")

        msg = "Expired JWT OutstandingToken cleanup completed"
        logger.info(
            "cleanup_task.event",
            detail_msg=msg,
        )
        return {"success": True, "message": msg}

    except Exception as e:
        logger.exception(
            "cleanup_task.failed",
            error=e,
        )
        raise


# =============================================================================
# Celery Task Registration
# =============================================================================

try:
    from celery import shared_task

    from baldur.settings.cleanup import get_cleanup_settings

    # Cache the settings at module load time
    _cleanup_settings = get_cleanup_settings()

    @shared_task(
        name="baldur.archive_old_dlq_entries",
        bind=True,
        max_retries=_cleanup_settings.archive_dlq_max_retries,
        default_retry_delay=_cleanup_settings.archive_dlq_retry_delay,
    )
    def archive_old_dlq_entries_task(self, older_than_days: int = 30):
        """Celery task wrapper for archive_old_dlq_entries."""
        return archive_old_dlq_entries(older_than_days)

    @shared_task(
        name="baldur.cleanup_expired_config",
        bind=True,
        max_retries=_cleanup_settings.expired_config_max_retries,
        default_retry_delay=_cleanup_settings.expired_config_retry_delay,
    )
    def cleanup_expired_config_task(self, older_than_hours: int = 24):
        """Celery task wrapper for cleanup_expired_config."""
        return cleanup_expired_config(older_than_hours)

    @shared_task(
        name="baldur.expire_approval_requests",
        bind=True,
        max_retries=_cleanup_settings.approval_max_retries,
        default_retry_delay=_cleanup_settings.approval_retry_delay,
    )
    def expire_approval_requests_task(self, older_than_hours: int = 72):
        """Celery task wrapper for expire_approval_requests."""
        return expire_approval_requests(older_than_hours)

    @shared_task(
        name="baldur.purge_archived_dlq_entries",
        bind=True,
        max_retries=_cleanup_settings.purge_dlq_max_retries,
        default_retry_delay=_cleanup_settings.purge_dlq_retry_delay,
    )
    def purge_archived_dlq_entries_task(
        self, older_than_days: int = 90, dry_run: bool = False
    ):
        """Celery task wrapper for purge_archived_dlq_entries."""
        return purge_archived_dlq_entries(older_than_days, dry_run)

    @shared_task(
        name="baldur.flush_expired_jwt_tokens",
        bind=True,
        max_retries=1,
        default_retry_delay=300,
    )
    def flush_expired_jwt_tokens_task(self):
        """Celery task wrapper for flush_expired_jwt_tokens."""
        return flush_expired_jwt_tokens()

    @shared_task(
        name="baldur.cleanup_stale_cb_keys",
        bind=True,
        max_retries=_cleanup_settings.cb_stale_key_max_retries,
        default_retry_delay=_cleanup_settings.cb_stale_key_retry_delay,
    )
    def cleanup_stale_cb_keys_task(self, retention_days: int | None = None):
        """Celery task wrapper for cleanup_stale_cb_keys."""
        return cleanup_stale_cb_keys(retention_days)

    @shared_task(
        name="baldur.cleanup_memory_cache_expired",
        bind=True,
        max_retries=1,
        default_retry_delay=600,
    )
    def cleanup_memory_cache_expired_task(self):
        """Celery task wrapper for cleanup_memory_cache_expired."""
        return cleanup_memory_cache_expired()

    @shared_task(
        name="baldur.refresh_audit_wal_metrics",
        bind=True,
        max_retries=1,
        default_retry_delay=600,
    )
    def refresh_audit_wal_metrics_task(self):
        """Celery task wrapper for refresh_audit_wal_metrics."""
        return refresh_audit_wal_metrics()

    CELERY_TASKS_AVAILABLE = True

except ImportError:
    logger.debug("cleanup_tasks.celery_available_skipping_task")
    CELERY_TASKS_AVAILABLE = False


# =============================================================================
# Beat Schedule definition
# =============================================================================


def get_cleanup_beat_schedule() -> dict[str, Any]:
    """
    Return the Cleanup Lane Beat Schedule.

    Returns:
        dict: Celery Beat Schedule configuration
    """
    try:
        from celery.schedules import crontab

        return {
            # Daily 02:30 - clean up expired config
            "cleanup-expired-config": {
                "task": "baldur.cleanup_expired_config",
                "schedule": crontab(hour=2, minute=30),
                "options": {"queue": "maintenance"},
                "kwargs": {"older_than_hours": 24},
            },
            # Daily 03:00 - DLQ archive
            "archive-old-dlq-entries": {
                "task": "baldur.archive_old_dlq_entries",
                "schedule": crontab(hour=3, minute=0),
                "options": {"queue": "maintenance"},
                "kwargs": {"older_than_days": 30},
            },
            # Daily 06:00 - expire approval requests
            "expire-approval-requests": {
                "task": "baldur.expire_approval_requests",
                "schedule": crontab(hour=6, minute=0),
                "options": {"queue": "maintenance"},
                "kwargs": {"older_than_hours": 72},
            },
            # Every Sunday 04:00 - DLQ permanent deletion (high-risk)
            "purge-archived-dlq-entries": {
                "task": "baldur.purge_archived_dlq_entries",
                "schedule": crontab(hour=4, minute=0, day_of_week=0),
                "options": {"queue": "critical_maintenance"},
                "kwargs": {"older_than_days": 90},
            },
            # Daily 02:30 - clean up expired JWT OutstandingTokens (#217)
            "flush-expired-jwt-tokens": {
                "task": "baldur.flush_expired_jwt_tokens",
                "schedule": crontab(hour=2, minute=30),
                "options": {"queue": "maintenance"},
            },
            # Daily 03:30 - clean up unused CB Redis keys (484 D5)
            "cleanup-stale-cb-keys": {
                "task": "baldur.cleanup_stale_cb_keys",
                "schedule": crontab(hour=3, minute=30),
                "options": {"queue": "maintenance"},
            },
            # Hourly :15 - clean up InMemoryCacheAdapter expired entries (484 D7)
            "cleanup-memory-cache-expired": {
                "task": "baldur.cleanup_memory_cache_expired",
                "schedule": crontab(minute=15),
                "options": {"queue": "maintenance"},
            },
            # Hourly :05 - refresh WAL disk pressure gauges (484 D11)
            "refresh-audit-wal-metrics": {
                "task": "baldur.refresh_audit_wal_metrics",
                "schedule": crontab(minute=5),
                "options": {"queue": "maintenance"},
            },
        }
    except ImportError:
        logger.debug("cleanup_tasks.celery_available_beat_schedule")
        return {}


__all__ = [
    # Thin wrapper functions
    "archive_old_dlq_entries",
    "cleanup_expired_config",
    "expire_approval_requests",
    "purge_archived_dlq_entries",
    "flush_expired_jwt_tokens",
    "cleanup_stale_cb_keys",
    "cleanup_memory_cache_expired",
    "refresh_audit_wal_metrics",
    # Beat schedule
    "get_cleanup_beat_schedule",
    # Service re-exports (for testing convenience)
    "CleanupResult",
    "CleanupService",
    "get_cleanup_service",
    "reset_cleanup_service",
]


# =============================================================================
# Lazy Service Re-exports
# =============================================================================


def __getattr__(name: str):
    """Lazy import for service types to avoid import chain issues."""
    _lazy_service_imports = {
        "CleanupResult",
        "CleanupService",
        "get_cleanup_service",
        "reset_cleanup_service",
    }
    if name in _lazy_service_imports:
        # Import directly to avoid services/__init__.py
        import importlib

        cs_module = importlib.import_module("baldur.services.cleanup_service")
        return getattr(cs_module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
