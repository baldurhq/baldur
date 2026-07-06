"""
Saga Settings - Pydantic v2.

Saga 오케스트레이터의 Lock heartbeat, TTL, stale 임계값,
Beat 스케줄, 기본 타임아웃 등을 환경변수로 외부화.

Source:
- services/saga/orchestrator.py (HEARTBEAT_INTERVAL, EXTEND_SECONDS, etc.)
- services/saga/tasks.py (STALE_THRESHOLD_SECONDS duplicate, Beat schedule)
- services/saga/models.py (SagaDefinition defaults)

Environment Variables:
    BALDUR_SAGA_ENABLED=true
    BALDUR_SAGA_LOCK_HEARTBEAT_INTERVAL_SECONDS=60
    BALDUR_SAGA_LOCK_EXTEND_SECONDS=300
    BALDUR_SAGA_MAX_RESUME_COUNT=10
    BALDUR_SAGA_STALE_THRESHOLD_SECONDS=300
    BALDUR_SAGA_DEFAULT_TIMEOUT_SECONDS=600
    BALDUR_SAGA_DEFAULT_MAX_RETRIES_PER_STEP=2
    BALDUR_SAGA_DEFAULT_RETRY_BACKOFF_STRATEGY=exponential
    BALDUR_SAGA_ORPHAN_SCAN_INTERVAL_SECONDS=120.0
    BALDUR_SAGA_RESUME_TASK_MAX_RETRIES=3
    BALDUR_SAGA_RESUME_TASK_RETRY_DELAY_SECONDS=60
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config
from baldur.settings.field_types import (
    STANDARD_RETRY_COUNT,
    MediumCount,
)


class SagaSettings(BaseSettings):
    """
    Saga Orchestrator 설정.

    Lock heartbeat, TTL, stale 임계값, Beat 스케줄,
    기본 타임아웃 등을 정의합니다.
    """

    model_config = make_settings_config("BALDUR_SAGA_")

    enabled: bool = Field(
        default=False,
        description="Enable/disable saga orchestrator.",
    )

    # =========================================================================
    # Lock management (services/saga/orchestrator.py)
    # =========================================================================
    lock_heartbeat_interval_seconds: int = Field(
        default=60,
        ge=5,
        le=600,
        description="Lock heartbeat polling interval (seconds).",
    )
    lock_extend_seconds: int = Field(
        default=300,
        ge=30,
        le=3600,
        description="Lock TTL extension amount (seconds).",
    )
    max_resume_count: MediumCount = Field(
        default=10,
        description="Maximum resume count to prevent infinite resumption.",
    )
    stale_threshold_seconds: int = Field(
        default=300,
        ge=30,
        le=3600,
        description="Orphan saga detection threshold (seconds).",
    )

    # =========================================================================
    # SagaDefinition defaults (services/saga/models.py)
    # =========================================================================
    default_timeout_seconds: int = Field(
        default=600,
        ge=30,
        le=86400,
        description="Default saga timeout (seconds).",
    )
    default_max_retries_per_step: int = Field(
        default=2,
        ge=0,
        le=20,
        description="Default max retries per saga step.",
    )
    default_retry_backoff_strategy: Literal[
        "exponential", "linear", "constant", "decorrelated"
    ] = Field(
        default="exponential",
        description="Default retry backoff strategy.",
    )

    # =========================================================================
    # Celery Beat schedule (services/saga/tasks.py)
    # =========================================================================
    orphan_scan_interval_seconds: float = Field(
        default=120.0,
        ge=10.0,
        le=3600.0,
        description="Orphan saga scan Beat interval (seconds).",
    )
    resume_task_max_retries: int = Field(
        default=STANDARD_RETRY_COUNT,
        ge=0,
        le=20,
        description="Max retries for resume_saga_instance_task.",
    )
    resume_task_retry_delay_seconds: int = Field(
        default=60,
        ge=5,
        le=600,
        description="Default retry delay for resume_saga_instance_task (seconds).",
    )

    # =========================================================================
    # Backpressure gate (409 UU-E4)
    # =========================================================================
    backpressure_rejection_level: Literal[
        "none", "low", "medium", "high", "critical"
    ] = Field(
        default="critical",
        description="Reject new sagas at this BackpressureLevel or above.",
    )

    @model_validator(mode="after")
    def validate_heartbeat_stale(self) -> SagaSettings:
        """Validate heartbeat vs stale threshold relationship."""
        if self.lock_heartbeat_interval_seconds >= self.stale_threshold_seconds:
            raise ValueError(
                f"lock_heartbeat_interval_seconds ({self.lock_heartbeat_interval_seconds}) "
                f"must be less than stale_threshold_seconds ({self.stale_threshold_seconds})"
            )
        # Minimum 3x ratio for network jitter / GC pause safety margin
        min_ratio = 3
        if (
            self.stale_threshold_seconds
            < self.lock_heartbeat_interval_seconds * min_ratio
        ):
            raise ValueError(
                f"stale_threshold_seconds ({self.stale_threshold_seconds}) should be at least "
                f"{min_ratio}x lock_heartbeat_interval_seconds ({self.lock_heartbeat_interval_seconds})"
            )
        return self


def get_saga_settings() -> SagaSettings:
    """Return cached SagaSettings via RootConfig."""
    from baldur.settings.root import get_config

    return get_config().services_group.saga


def reset_saga_settings() -> None:
    """Reset cached SagaSettings (for testing)."""
    from baldur.settings.root import get_config

    try:
        del get_config().services_group.__dict__["saga"]
    except KeyError:
        pass
