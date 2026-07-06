"""
Celery Task Settings - Pydantic v2.

Default configuration for Celery tasks.

Environment Variables:
    BALDUR_CELERY_TASK_MAX_RETRIES=3
    BALDUR_CELERY_TASK_DEFAULT_RETRY_DELAY=60
    BALDUR_CELERY_TASK_TIME_LIMIT=300
"""

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config
from baldur.settings.field_types import STANDARD_BACKOFF_MULTIPLIER, ShortInterval


class CeleryTaskSettings(BaseSettings):
    """
    Default configuration for Celery tasks.

    Retry strategy:
    - max_retries: maximum number of retries (3)
    - default_retry_delay: default retry delay (60s)
    - min_retry_delay: minimum retry delay (30s)
    - max_retry_delay: maximum retry delay (300s)
    - backoff_multiplier: exponential backoff multiplier (2)

    Time limits:
    - time_limit: hard timeout (300s)
    - soft_time_limit: soft timeout (240s)

    Note: soft_time_limit < time_limit must hold.
    """

    model_config = make_settings_config("BALDUR_CELERY_TASK_")

    # ==========================================================================
    # Retry Configuration - from tasks/*.py
    # ==========================================================================
    max_retries: int = Field(
        default=3,
        ge=0,
        le=10,
        description="Maximum number of retries",
    )

    default_retry_delay: int = Field(
        default=60,
        ge=10,
        le=600,
        description="Default retry delay (seconds)",
    )

    min_retry_delay: int = Field(
        default=30,
        ge=5,
        le=300,
        description="Minimum retry delay (seconds)",
    )

    max_retry_delay: int = Field(
        default=300,
        ge=60,
        le=3600,
        description="Maximum retry delay (seconds)",
    )

    backoff_multiplier: float = Field(
        default=STANDARD_BACKOFF_MULTIPLIER,
        ge=1.0,
        le=5.0,
        description="Exponential backoff multiplier",
    )

    # ==========================================================================
    # Time Limits - from tasks/*.py
    # ==========================================================================
    time_limit: int = Field(
        default=300,
        ge=30,
        le=3600,
        description="Hard timeout (seconds)",
    )

    soft_time_limit: int = Field(
        default=240,
        ge=20,
        le=3500,
        description="Soft timeout (seconds). Must be less than time_limit.",
    )

    # ==========================================================================
    # Rate Limiting - from tasks/*.py
    # ==========================================================================
    default_rate_limit: str = Field(
        default="10/s",
        pattern=r"^\d+/(s|m|h)$",
        description="Default rate limit (e.g., '10/s', '60/m')",
    )

    # ==========================================================================
    # Queue Configuration - default queue name
    # ==========================================================================
    default_queue: str = Field(
        default="baldur.default",
        description="Default queue name",
    )

    # ==========================================================================
    # Recovery Tasks Specific - from recovery_tasks.py
    # ==========================================================================
    trigger_check_interval: ShortInterval = Field(
        default=60,
        description="Trigger check interval (seconds)",
    )

    health_monitor_interval: int = Field(
        default=30,
        ge=10,
        le=120,
        description="Health monitor interval (seconds)",
    )

    stale_check_interval: ShortInterval = Field(
        default=10,
        description="Stale check interval (minutes)",
    )

    # ==========================================================================
    # Celery Inspector Timeout (from adapters/queues/celery_adapter.py)
    # ==========================================================================
    inspect_timeout: int = Field(
        default=2,
        ge=1,
        le=30,
        description="Celery inspect call timeout (seconds). Used for worker status checks.",
    )

    worker_status_cache_ttl_seconds: int = Field(
        default=15,
        ge=1,
        le=300,
        description="TTL (seconds) for the cached DLQ worker-presence probe used "
        "by the on-recovery auto-replay arming surface. Keeps the console's periodic "
        "stats polling from paying a broker round-trip on every request.",
    )

    # ==========================================================================
    # Queue Configuration (321 — Beat Internalization)
    # ==========================================================================
    queue_prefix: str = Field(
        default="",
        description="Queue namespace prefix (multi-service isolation). "
        "e.g., 'myapp' -> 'myapp.baldur.critical'",
    )

    queue_type: str = Field(
        default="quorum",
        pattern=r"^(classic|quorum|stream)$",
        description="RabbitMQ queue type (quorum recommended -- Raft-based message loss prevention)",
    )

    @model_validator(mode="after")
    def validate_time_limits(self) -> "CeleryTaskSettings":
        """Validate that soft_time_limit is less than time_limit."""
        if self.soft_time_limit >= self.time_limit:
            raise ValueError(
                f"soft_time_limit ({self.soft_time_limit}) must be less than "
                f"time_limit ({self.time_limit})"
            )
        if self.min_retry_delay > self.max_retry_delay:
            raise ValueError(
                f"min_retry_delay ({self.min_retry_delay}) must be less than or equal to "
                f"max_retry_delay ({self.max_retry_delay})"
            )
        return self


def get_celery_task_settings() -> "CeleryTaskSettings":
    from baldur.settings.root import get_config

    return get_config().adapters.celery_task


def reset_celery_task_settings() -> None:
    from baldur.settings.root import get_config

    try:
        del get_config().adapters.__dict__["celery_task"]
    except KeyError:
        pass
