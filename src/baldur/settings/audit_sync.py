"""
Audit Sync Settings - Pydantic v2.

Background Sync Worker settings (WAL → central store synchronization).

Source:
- audit/sync_worker.py (SyncWorkerConfig)

Environment Variables:
    BALDUR_AUDIT_SYNC_SYNC_INTERVAL_SECONDS=1.0
    BALDUR_AUDIT_SYNC_BATCH_SIZE=100
    BALDUR_AUDIT_SYNC_MAX_RETRIES=3
    BALDUR_AUDIT_SYNC_RETRY_DELAY_SECONDS=1.0
    BALDUR_AUDIT_SYNC_RETRY_BACKOFF_MULTIPLIER=2.0
    BALDUR_AUDIT_SYNC_MAX_RETRY_DELAY_SECONDS=30.0
    BALDUR_AUDIT_SYNC_CLEANUP_AFTER_SECONDS=3600.0
    BALDUR_AUDIT_SYNC_METRICS_INTERVAL_SECONDS=60.0
"""

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config
from baldur.settings.field_types import (
    STANDARD_BACKOFF_MULTIPLIER,
    STANDARD_BATCH_SIZE,
    LargeCount,
    ShortDuration,
)
from baldur.settings.validators import warn_below


class AuditSyncSettings(BaseSettings):
    """
    Audit Sync Worker settings.

    Settings for the background worker that syncs audit logs from the WAL to
    the central store. Implements fail-open plus a WAL-based zero-loss
    guarantee.
    """

    model_config = make_settings_config("BALDUR_AUDIT_SYNC_")

    # ==========================================================================
    # Sync Interval (from sync_worker.py line 42)
    # ==========================================================================
    sync_interval_seconds: ShortDuration = Field(
        default=1.0,
        description="Sync interval (seconds)",
    )

    # ==========================================================================
    # Batch Settings (from sync_worker.py line 45)
    # ==========================================================================
    batch_size: LargeCount = Field(
        default=STANDARD_BATCH_SIZE,
        description="Batch size",
    )

    # ==========================================================================
    # Retry Settings (from sync_worker.py lines 47-50)
    # ==========================================================================
    max_retries: int = Field(
        default=3,
        ge=0,
        le=10,
        description="Maximum number of retries",
    )
    retry_delay_seconds: float = Field(
        default=1.0,
        ge=0.1,
        le=30.0,
        description="Retry delay (seconds)",
    )
    retry_backoff_multiplier: float = Field(
        default=STANDARD_BACKOFF_MULTIPLIER,
        ge=1.0,
        le=5.0,
        description="Retry exponential backoff multiplier",
    )
    max_retry_delay_seconds: float = Field(
        default=30.0,
        ge=5.0,
        le=300.0,
        description="Maximum retry delay (seconds)",
    )

    # ==========================================================================
    # Cleanup Settings (from sync_worker.py line 53)
    # ==========================================================================
    cleanup_after_seconds: float = Field(
        default=3600.0,
        ge=300.0,
        le=86400.0,
        description="Threshold for cleaning up old entries (seconds)",
    )

    # ==========================================================================
    # Metrics Settings (from sync_worker.py line 56)
    # ==========================================================================
    metrics_interval_seconds: float = Field(
        default=60.0,
        ge=10.0,
        le=300.0,
        description="Metrics reporting interval (seconds)",
    )

    # ==========================================================================
    # Cursor Stall Alert
    # ==========================================================================
    cursor_stall_alert_cycles: int = Field(
        default=5,
        ge=1,
        le=1000,
        description=(
            "Consecutive failing sync batches where the contiguous cursor "
            "cannot advance (a permanently-failing head entry) before a "
            "CRITICAL cursor_stalled alert is emitted"
        ),
    )

    @field_validator("sync_interval_seconds")
    @classmethod
    def _warn_sync_interval_seconds(cls, v: float) -> float:
        """Warn when the sync interval is too short."""
        return warn_below(0.5, "audit_sync_settings.very_short_consider_using")(v)


# =============================================================================
# Singleton Pattern
# =============================================================================


def get_audit_sync_settings() -> "AuditSyncSettings":
    """
    Return the cached AuditSyncSettings instance.

    Returns:
        AuditSyncSettings: The singleton instance
    """
    from baldur.settings.root import get_config

    return get_config().audit_group.audit_sync


def reset_audit_sync_settings() -> None:
    """
    Reset the cached settings (for tests).
    """
    from baldur.settings.root import get_config

    try:
        del get_config().audit_group.__dict__["audit_sync"]
    except KeyError:
        pass
