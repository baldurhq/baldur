"""
Cleanup Settings - Pydantic v2.

DLQ, Pending Config, Approval cleanup task settings.

Source:
- services/cleanup_service.py
- tasks/cleanup_tasks.py

Environment Variables:
    # Cleanup criteria
    BALDUR_CLEANUP_ARCHIVE_OLDER_THAN_DAYS=30
    BALDUR_CLEANUP_EXPIRED_CONFIG_HOURS=24
    BALDUR_CLEANUP_APPROVAL_EXPIRY_HOURS=72
    BALDUR_CLEANUP_PURGE_OLDER_THAN_DAYS=90

    # Celery Task retry settings
    BALDUR_CLEANUP_ARCHIVE_DLQ_MAX_RETRIES=2
    BALDUR_CLEANUP_ARCHIVE_DLQ_RETRY_DELAY=300
    BALDUR_CLEANUP_EXPIRED_CONFIG_MAX_RETRIES=2
    BALDUR_CLEANUP_EXPIRED_CONFIG_RETRY_DELAY=300
    BALDUR_CLEANUP_APPROVAL_MAX_RETRIES=2
    BALDUR_CLEANUP_APPROVAL_RETRY_DELAY=300
    BALDUR_CLEANUP_PURGE_DLQ_MAX_RETRIES=1
    BALDUR_CLEANUP_PURGE_DLQ_RETRY_DELAY=600
"""

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config
from baldur.settings.validators import warn_below


class CleanupSettings(BaseSettings):
    """
    Cleanup task settings.

    Defines thresholds for DLQ archival, expired config cleanup,
    and approval request expiry.
    """

    model_config = make_settings_config("BALDUR_CLEANUP_")

    # ==========================================================================
    # DLQ Archive Settings (from cleanup_service.py line 65)
    # ==========================================================================
    archive_older_than_days: int = Field(
        default=30,
        ge=1,
        le=365,
        description="Resolved DLQ item archive threshold (days)",
    )

    # ==========================================================================
    # Expired Config Cleanup (from cleanup_service.py line 117)
    # ==========================================================================
    expired_config_hours: int = Field(
        default=24,
        ge=1,
        le=168,
        description="Expired Pending Config cleanup threshold (hours)",
    )

    # ==========================================================================
    # Approval Expiry (from cleanup_service.py line 165)
    # ==========================================================================
    approval_expiry_hours: int = Field(
        default=72,
        ge=1,
        le=336,
        description="Pending approval request expiry threshold (hours)",
    )

    # ==========================================================================
    # DLQ Purge Settings (from cleanup_service.py line 207)
    # ==========================================================================
    purge_older_than_days: int = Field(
        default=90,
        ge=30,
        le=730,
        description="Archived item permanent deletion threshold (days)",
    )

    # ==========================================================================
    # Celery Task retry settings (archive_old_dlq_entries_task)
    # ==========================================================================
    archive_dlq_max_retries: int = Field(
        default=2,
        ge=0,
        le=10,
        description="DLQ archive task max retry count",
    )
    archive_dlq_retry_delay: int = Field(
        default=300,
        ge=10,
        le=1800,
        description="DLQ archive task retry delay (seconds)",
    )

    # ==========================================================================
    # Celery Task retry settings (cleanup_expired_config_task)
    # ==========================================================================
    expired_config_max_retries: int = Field(
        default=2,
        ge=0,
        le=10,
        description="Expired config cleanup task max retry count",
    )
    expired_config_retry_delay: int = Field(
        default=300,
        ge=10,
        le=1800,
        description="Expired config cleanup task retry delay (seconds)",
    )

    # ==========================================================================
    # Celery Task retry settings (expire_approval_requests_task)
    # ==========================================================================
    approval_max_retries: int = Field(
        default=2,
        ge=0,
        le=10,
        description="Approval expiry task max retry count",
    )
    approval_retry_delay: int = Field(
        default=300,
        ge=10,
        le=1800,
        description="Approval expiry task retry delay (seconds)",
    )

    # ==========================================================================
    # Celery Task retry settings (purge_archived_dlq_entries_task)
    # High-risk operation, limited retries
    # ==========================================================================
    purge_dlq_max_retries: int = Field(
        default=1,
        ge=0,
        le=3,
        description="DLQ permanent deletion task max retry count (high-risk)",
    )
    purge_dlq_retry_delay: int = Field(
        default=600,
        ge=60,
        le=3600,
        description="DLQ permanent deletion task retry delay (seconds)",
    )

    # ==========================================================================
    # Recovery Session Cleanup (from recovery_tasks.py line 606)
    # ==========================================================================
    recovery_max_age_hours: int = Field(
        default=168,
        ge=24,
        le=720,
        description="Recovery session retention period (hours). Default 168 = 7 days",
    )

    # ==========================================================================
    # Approval Request Cleanup (from pending_recovery_approval.py line 537)
    # ==========================================================================
    approval_cleanup_max_age_hours: int = Field(
        default=24,
        ge=1,
        le=168,
        description="Completed approval request retention period (hours)",
    )

    # ==========================================================================
    # Approval Record Retention (from 443_LIFECYCLE_CLEANUP_GAPS D3)
    # ==========================================================================
    approval_record_retention_days: int = Field(
        default=7,
        ge=1,
        le=90,
        description="Days to retain terminal approval records (EXPIRED, APPROVED, REJECTED) before purging",
    )

    # ==========================================================================
    # CB stale-key cleanup (from 484_LIFECYCLE_HYGIENE_GAPS D5)
    # Orphan cb:{service_name} hashes left by service-rename / decom.
    # ==========================================================================
    cb_stale_key_retention_days: int = Field(
        default=30,
        ge=1,
        le=365,
        description="Delete CB state entries whose updated_at is older than this many days",
    )
    cb_stale_key_max_retries: int = Field(
        default=2,
        ge=0,
        le=10,
        description="CB stale-key cleanup task max retry count",
    )
    cb_stale_key_retry_delay: int = Field(
        default=300,
        ge=10,
        le=1800,
        description="CB stale-key cleanup task retry delay (seconds)",
    )

    @field_validator("purge_older_than_days")
    @classmethod
    def _warn_purge_older_than_days(cls, v: int, info) -> int:
        """purge_older_than_days should be greater than archive_older_than_days."""
        return warn_below(60, "cleanup_settings.low_consider_using_data")(v)


# =============================================================================
# Singleton Pattern
# =============================================================================


def get_cleanup_settings() -> "CleanupSettings":
    """
    Return cached CleanupSettings instance.

    Returns:
        CleanupSettings: singleton instance
    """
    from baldur.settings.root import get_config

    return get_config().services_group.cleanup


def reset_cleanup_settings() -> None:
    """
    Reset cached settings (for testing).

    Call this function to reload settings after environment variable changes.
    """
    from baldur.settings.root import get_config

    try:
        del get_config().services_group.__dict__["cleanup"]
    except KeyError:
        pass
