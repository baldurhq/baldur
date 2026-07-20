"""
arq Task Settings - Pydantic v2.

arq async task queue configuration for FastAPI and other
async-first frameworks.

Environment Variables:
    BALDUR_ARQ_TASK_ENABLED=false
    BALDUR_ARQ_TASK_REDIS_HOST=localhost
    BALDUR_ARQ_TASK_REDIS_PORT=6379
    BALDUR_ARQ_TASK_REDIS_DATABASE=1
    BALDUR_ARQ_TASK_MAX_JOBS=10
    BALDUR_ARQ_TASK_JOB_TIMEOUT=300
    BALDUR_ARQ_TASK_QUEUE_NAME=arq:baldur
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config
from baldur.settings.field_types import MediumCount, Probability


class ArqTaskSettings(BaseSettings):
    """
    arq async task queue settings.

    Redis connection:
        arq manages its own Redis connection via ArqRedis.
        Uses redis_database=1 by default to avoid conflict with
        Celery (which defaults to DB 0).

    Worker tuning:
        - max_jobs: concurrent tasks per worker process
        - job_timeout: hard timeout per task
        - max_tries: automatic retry count
        - retry_delay: base delay between retries
    """

    model_config = make_settings_config("BALDUR_ARQ_TASK_")

    enabled: bool = Field(
        default=False,
        description="Enable arq task adapter",
    )
    redis_host: str = Field(
        default="localhost",
        description="Redis host for arq",
    )
    redis_port: int = Field(
        default=6379,
        ge=1,
        le=65535,
        description="Redis port",
    )
    redis_database: int = Field(
        default=1,
        ge=0,
        le=15,
        description="Redis DB number (separate from Celery)",
    )
    redis_password: str | None = Field(
        default=None,
        description="Redis password",
    )
    redis_ssl: bool = Field(
        default=False,
        description="Enable Redis TLS",
    )
    max_jobs: MediumCount = Field(
        default=10,
        description="Max concurrent jobs per worker",
    )
    job_timeout: int = Field(
        default=300,
        ge=30,
        le=3600,
        description="Job timeout in seconds",
    )
    max_tries: int = Field(
        default=3,
        ge=0,
        le=10,
        description="Max retry attempts",
    )
    retry_delay: int = Field(
        default=60,
        ge=5,
        le=600,
        description="Retry delay in seconds",
    )
    queue_name: str = Field(
        default="arq:baldur",
        description="Default queue name",
    )
    health_check_interval: int = Field(
        default=60,
        ge=10,
        le=300,
        description="Health check interval in seconds",
    )
    keep_result: int = Field(
        default=3600,
        ge=60,
        le=86400,
        description="Result TTL in seconds",
    )
    enqueue_batch_size: int = Field(
        default=100,
        ge=10,
        le=1000,
        description="Max concurrent enqueue coroutines per gather batch",
    )
    enqueue_failure_threshold: Probability = Field(
        default=0.5,
        description="Chunk failure ratio to abort remaining chunks (0.5 = 50%)",
    )


def get_arq_task_settings() -> ArqTaskSettings:
    """Get singleton ArqTaskSettings instance."""
    from baldur.runtime import get_runtime

    return get_runtime().get_settings(ArqTaskSettings)


def reset_arq_task_settings() -> None:
    """Reset ArqTaskSettings singleton (for testing)."""
    from baldur.runtime import get_runtime

    get_runtime().reset_settings(ArqTaskSettings)
