"""Unified checkpoint storage strategy package.

Pluggable storage strategies for checkpoint persistence:
- FileCheckpointStorage: Pure Python, no dependencies (small-scale)
- RedisCheckpointStorage: Redis-based (medium-scale)
- KafkaRedisCheckpointStorage: Kafka+Redis (enterprise)
- CompositeCheckpointStorage: Multi-storage fallback chain

Usage:
    from baldur.audit.checkpoint import (
        get_checkpoint_strategy,
        CheckpointStorageStrategy,
    )

    # Environment-based auto-selection
    strategy = get_checkpoint_strategy()

    # Explicit selection
    strategy = get_checkpoint_strategy(storage_type="redis")

    # Save checkpoint
    strategy.save("default", UnifiedCheckpointData(wal_sequence=1234))

    # Load checkpoint
    data = strategy.load("default")

Version: 1.0.0
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from baldur.audit.checkpoint.composite_storage import CompositeCheckpointStorage
from baldur.audit.checkpoint.file_lock import (
    FILE_LOCK_RETRY_INTERVAL,
    FILE_LOCK_TIMEOUT_SECONDS,
    lock_file,
    unlock_file,
)
from baldur.audit.checkpoint.file_storage import FileCheckpointStorage
from baldur.audit.checkpoint.kafka_redis_storage import (
    KafkaRedisCheckpointStorage,
)
from baldur.audit.checkpoint.redis_storage import RedisCheckpointStorage
from baldur.audit.checkpoint.strategy import (
    UNINITIALIZED,
    CheckpointCorruptedError,
    CheckpointError,
    CheckpointStorageStrategy,
    UnifiedCheckpointData,
    get_load_failures_counter,
    get_save_failures_counter,
)

if TYPE_CHECKING:
    import redis

logger = structlog.get_logger()

__all__ = [
    # file_lock
    "FILE_LOCK_RETRY_INTERVAL",
    "FILE_LOCK_TIMEOUT_SECONDS",
    "lock_file",
    "unlock_file",
    # strategy (base)
    "UNINITIALIZED",
    "CheckpointCorruptedError",
    "CheckpointError",
    "CheckpointStorageStrategy",
    "UnifiedCheckpointData",
    "get_load_failures_counter",
    "get_save_failures_counter",
    # storage implementations
    "CompositeCheckpointStorage",
    "FileCheckpointStorage",
    "KafkaRedisCheckpointStorage",
    "RedisCheckpointStorage",
    # registry
    "CheckpointStrategyRegistry",
    # factory / singleton
    "get_checkpoint_strategy",
    "get_default_checkpoint_strategy",
    "configure_default_checkpoint_strategy",
    "reset_default_checkpoint_strategy",
]


# =============================================================================
# Strategy Registry (dynamic strategy registration)
# =============================================================================


class CheckpointStrategyRegistry:
    """
    Checkpoint strategy dynamic registry.

    Usage:
        # Register custom strategy
        CheckpointStrategyRegistry.register("s3", S3CheckpointStorage)
        CheckpointStrategyRegistry.register("gcs", GCSCheckpointStorage)

        # Get strategy
        strategy = CheckpointStrategyRegistry.get("s3", bucket="my-bucket")

        # Set default
        CheckpointStrategyRegistry.set_default("redis")
    """

    _strategies: dict[str, type[CheckpointStorageStrategy]] = {}
    _instances: dict[str, CheckpointStorageStrategy] = {}
    _default: str = "file"
    _lock = threading.Lock()

    @classmethod
    def register(
        cls, name: str, strategy_class: type[CheckpointStorageStrategy]
    ) -> None:
        """
        Register strategy.

        Args:
            name: Strategy identifier (e.g. "file", "redis", "s3")
            strategy_class: CheckpointStorageStrategy implementation
        """
        with cls._lock:
            cls._strategies[name] = strategy_class
            logger.info(
                "checkpoint_registry.strategy_registered",
                strategy_name=name,
            )

    @classmethod
    def get(
        cls,
        name: str | None = None,
        force_new: bool = False,
        **kwargs,
    ) -> CheckpointStorageStrategy:
        """
        Get strategy instance.

        Args:
            name: Strategy name (None for default)
            force_new: Force new instance creation
            **kwargs: Strategy constructor arguments
        """
        name = name or cls._default

        with cls._lock:
            if name not in cls._strategies:
                cls._auto_register()
                if name not in cls._strategies:
                    raise ValueError(f"Unknown checkpoint strategy: {name}")

            if force_new or name not in cls._instances:
                cls._instances[name] = cls._strategies[name](**kwargs)

            return cls._instances[name]

    @classmethod
    def set_default(cls, name: str) -> None:
        """Set default strategy."""
        with cls._lock:
            cls._default = name

    @classmethod
    def list_strategies(cls) -> list[str]:
        """List registered strategies."""
        cls._auto_register()
        return list(cls._strategies.keys())

    @classmethod
    def _auto_register(cls) -> None:
        """Auto-register built-in strategies."""
        if "file" not in cls._strategies:
            cls._strategies["file"] = FileCheckpointStorage
        if "redis" not in cls._strategies:
            cls._strategies["redis"] = RedisCheckpointStorage
        if "kafka_redis" not in cls._strategies:
            cls._strategies["kafka_redis"] = KafkaRedisCheckpointStorage
        if "composite" not in cls._strategies:
            cls._strategies["composite"] = CompositeCheckpointStorage

    @classmethod
    def clear(cls) -> None:
        """Clear registry (for testing)."""
        with cls._lock:
            cls._strategies.clear()
            cls._instances.clear()
            cls._default = "file"


# =============================================================================
# K8s environment detection
# =============================================================================


def _is_k8s_environment() -> bool:
    """Detect K8s environment."""
    return bool(
        os.environ.get("KUBERNETES_SERVICE_HOST")
        or os.environ.get("KUBERNETES_PORT")
        or Path("/var/run/secrets/kubernetes.io").exists()
    )


# =============================================================================
# Factory function
# =============================================================================


def get_checkpoint_strategy(  # noqa: C901, PLR0912
    storage_type: str | None = None,
    redis_client: redis.Redis | None = None,
    **kwargs,
) -> CheckpointStorageStrategy:
    """
    Return checkpoint storage strategy appropriate for the environment.

    Args:
        storage_type: Storage type ("file", "redis", "kafka_redis", "composite")
                     None reads from BALDUR_CHECKPOINT_STORAGE env var
        redis_client: Redis client (required for redis, kafka_redis)
        **kwargs: Additional settings

    Returns:
        CheckpointStorageStrategy implementation

    Environment variables:
        BALDUR_CHECKPOINT_STORAGE: file, redis, kafka_redis, composite
        BALDUR_AUDIT_PATH: File storage path
        BALDUR_CHECKPOINT_ENABLE_NOTIFICATION: Enable notifications (default TRUE)
        BALDUR_CHECKPOINT_USE_DISTRIBUTED_LOCK: Use distributed lock (default TRUE)
        BALDUR_CHECKPOINT_ENABLE_FILE_BACKUP: Use file backup (default TRUE)

    Usage:
        # Environment-based auto-selection
        strategy = get_checkpoint_strategy()

        # Explicit selection
        strategy = get_checkpoint_strategy(storage_type="redis", redis_client=r)

        # CompositeCheckpointStorage usage
        strategy = get_checkpoint_strategy(
            storage_type="composite",
            redis_client=r,
            primary_type="redis",
            secondary_type="file",
        )
    """
    if storage_type is None:
        storage_type = os.environ.get("BALDUR_CHECKPOINT_STORAGE", "file").lower()

    # Warn when using file mode in K8s
    if storage_type == "file" and _is_k8s_environment():
        logger.warning(
            "checkpoint_strategy.file_storage_in_k8s",
            message="FileCheckpointStorage is not recommended in multi-pod K8s environments. "
            "Use BALDUR_CHECKPOINT_STORAGE=redis or composite for cross-pod consistency.",
        )

    # Load options from environment variables
    enable_notification = (
        os.environ.get("BALDUR_CHECKPOINT_ENABLE_NOTIFICATION", "TRUE").upper()
        == "TRUE"
    )
    use_distributed_lock = (
        os.environ.get("BALDUR_CHECKPOINT_USE_DISTRIBUTED_LOCK", "TRUE").upper()
        == "TRUE"
    )
    enable_file_backup = (
        os.environ.get("BALDUR_CHECKPOINT_ENABLE_FILE_BACKUP", "TRUE").upper() == "TRUE"
    )

    if storage_type == "file":
        return FileCheckpointStorage(
            base_path=kwargs.get("base_path"),
            sync_on_write=kwargs.get("sync_on_write", True),
        )

    if storage_type == "redis":
        if redis_client is None:
            raise ValueError("redis_client is required for storage_type='redis'")
        return RedisCheckpointStorage(
            redis_client=redis_client,
            ttl_seconds=kwargs.get("ttl_seconds"),
            use_distributed_lock=kwargs.get(
                "use_distributed_lock", use_distributed_lock
            ),
            enable_notification=kwargs.get("enable_notification", enable_notification),
        )

    if storage_type == "kafka_redis":
        if redis_client is None:
            raise ValueError("redis_client is required for storage_type='kafka_redis'")
        return KafkaRedisCheckpointStorage(
            redis_client=redis_client,
            default_topic=kwargs.get("default_topic", "baldur.audit.events"),
            enable_file_backup=kwargs.get("enable_file_backup", enable_file_backup),
            enable_notification=kwargs.get("enable_notification", enable_notification),
        )

    if storage_type == "composite":
        # Composite strategy creation
        primary_type = kwargs.get("primary_type", "redis")
        secondary_type = kwargs.get("secondary_type", "file")

        primary: CheckpointStorageStrategy
        if primary_type == "redis":
            if redis_client is None:
                raise ValueError(
                    "redis_client is required for composite with redis primary"
                )
            primary = RedisCheckpointStorage(
                redis_client=redis_client,
                use_distributed_lock=use_distributed_lock,
                enable_notification=False,  # Managed by Composite
            )
        else:
            primary = FileCheckpointStorage()

        secondary = (
            FileCheckpointStorage(
                base_path=kwargs.get("secondary_base_path"),
            )
            if secondary_type == "file"
            else None
        )

        return CompositeCheckpointStorage(
            primary=primary,
            secondary=secondary,
            enable_memory_fallback=kwargs.get("enable_memory_fallback", True),
        )

    # Try registry lookup
    try:
        return CheckpointStrategyRegistry.get(storage_type, **kwargs)
    except ValueError as _err:
        raise ValueError(f"Unknown storage_type: {storage_type}") from _err


# =============================================================================
# Singleton
# =============================================================================


def _create_default_checkpoint_strategy() -> CheckpointStorageStrategy:
    """Create default CheckpointStorageStrategy based on environment variables."""
    storage_type = os.environ.get("BALDUR_CHECKPOINT_STORAGE", "file")

    if storage_type in ("redis", "kafka_redis"):
        # RedisCheckpointStorage handles client acquisition via Optional DI
        try:
            return get_checkpoint_strategy(storage_type=storage_type)
        except (ImportError, ValueError):
            logger.warning("checkpoint_strategy.redis_unavailable")
            return FileCheckpointStorage()
    else:
        return FileCheckpointStorage()


from baldur.utils.singleton import make_singleton_factory  # noqa: E402

(
    get_default_checkpoint_strategy,
    configure_default_checkpoint_strategy,
    reset_default_checkpoint_strategy,
) = make_singleton_factory(
    "default_checkpoint_strategy", _create_default_checkpoint_strategy
)
