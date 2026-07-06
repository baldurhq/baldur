"""
Runbook Executor Settings - Pydantic v2.

Settings for the automated runbook executor.

Configuration items:
- Whether the runbook system is enabled
- Limit on concurrently executing runbooks
- Default per-step timeout
- Distributed lock TTL
- MEDIUM-risk timer auto-approval time
- HIGH-risk maximum wait time

Environment Variables:
    BALDUR_RUNBOOK_ENABLED=true
    BALDUR_RUNBOOK_MAX_CONCURRENT_RUNBOOKS=3
    BALDUR_RUNBOOK_STEP_DEFAULT_TIMEOUT_SECONDS=120
    BALDUR_RUNBOOK_LOCK_TTL_SECONDS=600
    BALDUR_RUNBOOK_APPROVAL_TIMER_SECONDS=300
    BALDUR_RUNBOOK_APPROVAL_MAX_WAIT_SECONDS=3600

Reference:
- docs/baldur/middleware_system/272_RUNBOOK_ARCHITECTURE_OVERVIEW.md §7
"""

import structlog
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config
from baldur.settings.field_types import (
    MediumCount,
    SmallCount,
)

logger = structlog.get_logger()


class RunbookSettings(BaseSettings):
    """
    Automated runbook executor settings.

    Manages the configuration of the orchestration layer that declaratively
    matches recovery procedures to failure patterns and runs them automatically.

    Features:
    - Global enable/disable for the runbook system
    - Auto-approval wait time for MEDIUM-risk runbooks
    - Resource protection via a concurrent runbook limit
    - Infinite-wait prevention via a default per-step timeout
    - Deadlock prevention via a distributed lock TTL
    """

    model_config = make_settings_config("BALDUR_RUNBOOK_")

    # ==========================================================================
    # Global Toggle
    # ==========================================================================
    enabled: bool = Field(
        default=False,
        description="Enable runbook system",
    )

    # ==========================================================================
    # Concurrency Settings
    # ==========================================================================
    max_concurrent_runbooks: SmallCount = Field(
        default=3,
        description="Maximum number of concurrently executing runbooks",
    )

    # ==========================================================================
    # Timeout Settings
    # ==========================================================================
    step_default_timeout_seconds: int = Field(
        default=120,
        ge=10,
        le=1800,
        description="Default timeout for runbook steps (seconds)",
    )

    # ==========================================================================
    # Distributed Lock Settings
    # ==========================================================================
    lock_ttl_seconds: int = Field(
        default=600,
        ge=60,
        le=7200,
        description="Distributed lock TTL (seconds). Prevents concurrent recovery during runbook execution",
    )

    # ==========================================================================
    # Executor Settings (doc #275 Executor)
    # ==========================================================================
    global_timeout_seconds: int = Field(
        default=1800,
        ge=60,
        le=86400,
        description="Global runbook execution timeout (seconds). Default 30 minutes",
    )

    lock_extend_seconds: int = Field(
        default=300,
        ge=60,
        le=3600,
        description="Default lock TTL extension (seconds). Heartbeat extension amount",
    )

    lock_heartbeat_interval: int = Field(
        default=60,
        ge=10,
        le=600,
        description="Lock heartbeat polling interval (seconds). Same as SagaOrchestrator.HEARTBEAT_INTERVAL",
    )

    idempotency_ttl_hours: int = Field(
        default=24,
        ge=1,
        le=168,
        description="Idempotency key TTL (hours)",
    )

    context_ttl_seconds: int = Field(
        default=86400,
        ge=3600,
        le=604800,
        description="Execution context persistence TTL (seconds). Default 24 hours",
    )

    resume_stale_threshold_seconds: int = Field(
        default=3600,
        ge=60,
        le=86400,
        description="Stale rejection threshold on resume (seconds). Default 1 hour",
    )

    max_resume_count: MediumCount = Field(
        default=10,
        description="Infinite resume prevention counter. Same as SagaOrchestrator.MAX_RESUME_COUNT",
    )

    # ==========================================================================
    # Approval Gate Settings (doc #276 ApprovalGate)
    # ==========================================================================
    approval_timer_seconds: int = Field(
        default=300,
        ge=30,
        le=7200,
        description="Timer-based auto-approval wait time for MEDIUM risk runbooks (seconds). Default 5 minutes",
    )

    approval_max_wait_seconds: int = Field(
        default=3600,
        ge=0,
        le=86400,
        description="Maximum wait time for HIGH risk runbooks (seconds). 0 means indefinite wait. Default 1 hour",
    )

    approval_reminder_intervals_minutes: list[int] = Field(
        default=[15, 30],
        description="Reminder intervals during approval waiting (list in minutes)",
    )

    approval_check_interval_seconds: int = Field(
        default=30,
        ge=10,
        le=300,
        description="Celery Beat polling interval for timer/reminder/timeout checks (seconds)",
    )

    force_execute_audit_required: bool = Field(
        default=True,
        description="Require audit log for CRITICAL runbook force execution",
    )

    # ==========================================================================
    # RunbookService Settings (doc #278 Service)
    # ==========================================================================
    async_execution: bool = Field(
        default=True,
        description="True: async execution via Celery task, False: sync execution (dev/test only)",
    )

    event_priority: str = Field(
        default="LOW",
        description="EventBus subscription priority",
    )

    subscribe_events: bool = Field(
        default=True,
        description="EventBus subscription toggle. Recommended True for Celery workers, False for web servers",
    )

    orphan_scan_interval_seconds: int = Field(
        default=120,
        ge=30,
        le=600,
        description="Orphan scan beat interval (seconds). Default 2 minutes",
    )

    orphan_stale_threshold_seconds: int = Field(
        default=600,
        ge=60,
        le=7200,
        description="EXECUTING state orphan detection threshold (seconds). Default 10 minutes",
    )

    @field_validator("approval_timer_seconds")
    @classmethod
    def validate_approval_timer(cls, v: int) -> int:
        """Warn on the MEDIUM-risk timer auto-approval wait time."""
        if v < 60:
            logger.warning(
                "runbook.approval_timer_too_short",
                seconds=v,
                msg="A short approval wait time may auto-approve without review",
            )
        if v > 1800:
            logger.warning(
                "runbook.approval_timer_too_long",
                seconds=v,
                msg="A long approval wait time may delay failure recovery",
            )
        return v

    @field_validator("lock_ttl_seconds")
    @classmethod
    def validate_lock_ttl(cls, v: int) -> int:
        """Warn that the lock TTL must be sufficiently larger than the step timeout."""
        if v < 120:
            logger.warning(
                "runbook.lock_ttl_too_short",
                seconds=v,
                msg="A short lock TTL may let the lock expire mid-execution",
            )
        return v


# =============================================================================
# Singleton Pattern
# =============================================================================


def get_runbook_settings() -> "RunbookSettings":
    """Get cached RunbookSettings instance."""
    from baldur.settings.root import get_config

    return get_config().services_group.runbook


def reset_runbook_settings() -> None:
    """Reset cached settings (for testing)."""
    from baldur.settings.root import get_config

    try:
        del get_config().services_group.__dict__["runbook"]
    except KeyError:
        pass
