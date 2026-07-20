"""
Batch Settings - Pydantic v2.

Batch processing size and flush interval settings.

Replaces:
- services/async_logger.py:BATCH_SIZE, FLUSH_INTERVAL
- services/dlq_models.py:batch_size
- services/dlq/replay_operations.py:batch_size
- coordination/redis_key_guard.py:batch_size

Environment Variables:
    BALDUR_BATCH_DEFAULT_BATCH_SIZE=100
    BALDUR_BATCH_LOGGER_BATCH_SIZE=10
    BALDUR_BATCH_FLUSH_INTERVAL=5.0
"""

from pydantic import Field
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config
from baldur.settings.field_types import (
    STANDARD_BATCH_SIZE,
    HugeCount,
    ShortDuration,
)


class BatchSettings(BaseSettings):
    """
    Batch processing settings.

    Batch sizes by purpose:
    - default_batch_size: general batch operations (100)
    - logger_batch_size: async logging batch (10)
    - dlq_batch_size: DLQ replay batch (50)
    - redis_scan_batch_size: Redis scan batch (100)
    """

    model_config = make_settings_config("BALDUR_BATCH_")

    # ==========================================================================
    # Default Batch Size - multiple files
    # ==========================================================================
    default_batch_size: int = Field(
        default=STANDARD_BATCH_SIZE,
        ge=10,
        le=1000,
        description="General batch operation default size",
    )

    # ==========================================================================
    # Logger Batch - from async_logger.py
    # ==========================================================================
    logger_batch_size: HugeCount = Field(
        default=STANDARD_BATCH_SIZE,
        description=(
            "AsyncHealingLogger batch size. "
            "Recommend 1,000+ for high-volume processing. "
            "Too large increases memory, too small increases I/O."
        ),
    )

    # ==========================================================================
    # Flush Interval - from async_logger.py
    # ==========================================================================
    flush_interval: ShortDuration = Field(
        default=5.0,
        description=(
            "Batch flush interval (seconds). Recommend 1.0-2.0 for high-speed. "
            "0.5 possible for real-time requirements."
        ),
    )

    # ==========================================================================
    # DLQ Batch - from dlq/replay_operations.py
    # ==========================================================================
    dlq_batch_size: int = Field(
        default=50,
        ge=10,
        le=500,
        description="DLQ replay batch size",
    )

    # ==========================================================================
    # Redis Scan Batch - from redis_key_guard.py
    # ==========================================================================
    redis_scan_batch_size: int = Field(
        default=STANDARD_BATCH_SIZE,
        ge=50,
        le=1000,
        description="Redis SCAN command batch size",
    )

    # ==========================================================================
    # Audit Batch - from audit/config.py
    # ==========================================================================
    audit_batch_size: int = Field(
        default=STANDARD_BATCH_SIZE,
        ge=10,
        le=500,
        description="Audit log batch size",
    )

    audit_flush_interval: float = Field(
        default=10.0,
        ge=1.0,
        le=60.0,
        description="Audit log flush interval (seconds)",
    )

    # ==========================================================================
    # Async Logger Config - from audit/audit_integration.py AsyncLoggerConfig
    # ==========================================================================
    async_logger_batch_size: int = Field(
        default=5,
        ge=1,
        le=100,
        description="AsyncLogger batch size",
    )

    async_logger_flush_interval: float = Field(
        default=2.0,
        ge=0.5,
        le=30.0,
        description="AsyncLogger flush interval (seconds)",
    )

    async_logger_max_queue_size: int = Field(
        default=5000,
        ge=100,
        le=100000,
        description="AsyncLogger max queue size",
    )


# ==========================================================================
# Singleton management
# ==========================================================================
def get_batch_settings() -> "BatchSettings":
    """Get cached BatchSettings instance."""
    from baldur.runtime import get_runtime

    return get_runtime().get_settings(BatchSettings)


def reset_batch_settings() -> None:
    """Reset cached settings (for testing)."""
    from baldur.runtime import get_runtime

    get_runtime().reset_settings(BatchSettings)
