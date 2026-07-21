"""Kafka+Redis-based checkpoint storage implementation.

Enterprise-grade storage combining WAL sequence + Kafka offset atomic saves.
Uses Redis for fast lookups and file backup for Redis failure recovery.

Version: 1.0.0
"""

from __future__ import annotations

import os
import tempfile
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from baldur.audit.checkpoint.file_storage import FileCheckpointStorage
from baldur.audit.checkpoint.strategy import (
    CheckpointCorruptedError,
    CheckpointError,
    CheckpointStorageStrategy,
    UnifiedCheckpointData,
)
from baldur.core.exceptions import ConfigurationError
from baldur.utils.serialization import fast_dumps_str, fast_loads

if TYPE_CHECKING:
    import redis

__all__ = [
    "KafkaRedisCheckpointStorage",
]

logger = structlog.get_logger()


class KafkaRedisCheckpointStorage(CheckpointStorageStrategy):
    """
    Kafka+Redis-based checkpoint storage.

    Features:
    - WAL sequence + Kafka offset atomic save
    - Redis for fast lookups, Kafka offset for accurate recovery
    - File backup for Redis failure recovery
    - Checksum verification and notification integration
    """

    KEY_PREFIX = "baldur:kafka_checkpoint:"

    # Distinct from FileCheckpointStorage's own purpose: both stores write
    # ``checkpoint.{namespace}.json`` with no role discriminator in the
    # filename, so a shared purpose would merge the Redis-failure backup
    # into the primary store's fallback directory and contend on its locks.
    BACKUP_PURPOSE = "checkpoint_kafka_backup"

    def __init__(
        self,
        redis_client: redis.Redis,
        default_topic: str = "baldur.audit.events",
        file_backup_path: str | Path | None = None,
        enable_file_backup: bool = True,
        enable_notification: bool = True,
    ):
        """
        Initialize Kafka+Redis checkpoint storage.

        Args:
            redis_client: Redis client
            default_topic: Default Kafka topic
            file_backup_path: File backup path (for Redis failure recovery)
            enable_file_backup: Enable file backup (recommended: True)
            enable_notification: Enable failure notifications
        """
        super().__init__()
        # redis-py stub returns Awaitable[X] | X dual unions; widening to Any
        # keeps mypy out of every sync call site (per state_backend precedent).
        self._redis: Any = redis_client
        self._default_topic = default_topic
        self._enable_file_backup = enable_file_backup
        self._enable_notification = enable_notification
        self._local_lock = threading.Lock()

        # File backup storage (for Redis failure recovery)
        self._file_backup: FileCheckpointStorage | None = None
        if enable_file_backup:
            backup_path = file_backup_path or self._get_default_backup_path()
            operator_set = bool(file_backup_path) or bool(
                os.environ.get(FileCheckpointStorage.PATH_ENV_VAR)
            )
            try:
                self._file_backup = FileCheckpointStorage(
                    base_path=backup_path,
                    base_path_operator_set=operator_set,
                    purpose=self.BACKUP_PURPOSE,
                )
            except ConfigurationError as e:
                # Optional integration -> fail-open. Enabling the
                # Redis-failure backup must not fail construction where
                # plain Redis storage would have worked.
                logger.warning(
                    "kafka_redis_checkpoint.file_backup_disabled",
                    backup_path=str(backup_path),
                    error=str(e),
                )
            else:
                logger.info(
                    "kafka_redis_checkpoint.file_backup_enabled",
                    backup_path=str(self._file_backup.base_path),
                )

    def _get_default_backup_path(self) -> Path:
        """Get default backup path."""
        env_path = os.environ.get(FileCheckpointStorage.PATH_ENV_VAR)
        if env_path:
            return Path(env_path) / "kafka_checkpoint_backup"
        if os.name == "nt":
            return Path(tempfile.gettempdir()) / "baldur" / "kafka_checkpoint_backup"
        return Path("/var/log/audit/kafka_checkpoint_backup")

    def _get_key(self, namespace: str) -> str:
        """Generate Redis key."""
        return f"{self.KEY_PREFIX}{namespace}"

    def save(self, namespace: str, data: UnifiedCheckpointData) -> None:
        """
        Save checkpoint (Redis Primary + File Backup).

        Save order:
        1. Save to Redis (Primary)
        2. Save to File backup (Secondary) - for Redis failure recovery
        """
        # Resolve Kafka field defaults without mutating input data
        kafka_topic = (
            data.kafka_topic if data.kafka_topic is not None else self._default_topic
        )
        kafka_partition = (
            data.kafka_partition if data.kafka_partition is not None else 0
        )
        kafka_offset = data.kafka_offset if data.kafka_offset is not None else 0

        redis_success = False
        file_success = False

        # 1. Redis save (Primary)
        try:
            key = self._get_key(namespace)
            redis_data = UnifiedCheckpointData(
                wal_sequence=data.wal_sequence,
                timestamp=data.timestamp,
                version=data.version,
                kafka_topic=kafka_topic,
                kafka_partition=kafka_partition,
                kafka_offset=kafka_offset,
                checksum=data.checksum,
            )
            value = fast_dumps_str(redis_data.to_dict())
            self._redis.set(key, value)
            redis_success = True
            logger.debug(
                "kafka_redis_checkpoint.redis_saved",
                namespace=namespace,
                data=data.wal_sequence,
                kafka_offset=kafka_offset,
            )
        except Exception as e:
            logger.exception(
                "kafka_redis_checkpoint.redis_save_failed",
                error=e,
            )

        # 2. File backup (Secondary) - always attempt regardless of Redis
        if self._file_backup:
            try:
                self._file_backup.save(namespace, data)
                file_success = True
                logger.debug(
                    "kafka_redis_checkpoint.file_backup_saved",
                    namespace=namespace,
                )
            except Exception as e:
                logger.warning(
                    "kafka_redis_checkpoint.file_backup_failed",
                    error=e,
                )

        # Both failed: notify + raise
        if not redis_success and not file_success:
            self._notify_failure(namespace, "Both Redis and File storage failed")
            raise CheckpointError("All checkpoint storage tiers failed")

        # Only Redis failed: warning notification
        if not redis_success:
            self._notify_degraded(namespace)

    def _notify_failure(self, namespace: str, error: str) -> None:
        """Checkpoint save failure notification (CRITICAL)."""
        if not self._enable_notification:
            return

        try:
            from baldur_pro.services.unified_notification import (
                NotificationCategory,
                NotificationPayload,
                NotificationPriority,
                UnifiedNotificationManager,
            )

            manager = UnifiedNotificationManager()
            manager.notify(
                NotificationPayload(
                    title="Kafka Checkpoint Save Failed",
                    message=f"All storage tiers failed for namespace '{namespace}': {error}",
                    priority=NotificationPriority.CRITICAL,
                    category=NotificationCategory.OPERATIONS,
                    source="kafka_redis_checkpoint",
                    metadata={"namespace": namespace, "error": error},
                )
            )
        except Exception as e:
            logger.warning(
                "kafka_redis_checkpoint.send_failure_notification_failed",
                error=e,
            )

    def _notify_degraded(self, namespace: str) -> None:
        """Degraded state notification due to Redis failure (HIGH)."""
        if not self._enable_notification:
            return

        try:
            from baldur_pro.services.unified_notification import (
                NotificationCategory,
                NotificationPayload,
                NotificationPriority,
                UnifiedNotificationManager,
            )

            manager = UnifiedNotificationManager()
            manager.notify(
                NotificationPayload(
                    title="Kafka Checkpoint Degraded",
                    message=f"Redis unavailable, using file backup for namespace '{namespace}'",
                    priority=NotificationPriority.HIGH,
                    category=NotificationCategory.OPERATIONS,
                    source="kafka_redis_checkpoint",
                    metadata={"namespace": namespace, "tier": "file_backup"},
                )
            )
        except Exception as e:
            logger.warning(
                "kafka_redis_checkpoint.send_degraded_notification_failed",
                error=e,
            )

    def save_with_kafka_offset(
        self,
        namespace: str,
        wal_sequence: int,
        kafka_topic: str,
        kafka_partition: int,
        kafka_offset: int,
        checksum: str | None = None,
    ) -> None:
        """
        Save checkpoint with Kafka offset.
        """
        data = UnifiedCheckpointData(
            wal_sequence=wal_sequence,
            kafka_topic=kafka_topic,
            kafka_partition=kafka_partition,
            kafka_offset=kafka_offset,
            checksum=checksum,
        )
        self.save(namespace, data)

    def load(self, namespace: str) -> UnifiedCheckpointData | None:
        """Load checkpoint (Redis -> File Fallback + Checksum verification)."""
        # 1. Try Redis
        try:
            key = self._get_key(namespace)
            raw = self._redis.get(key)
            if raw:
                raw_data = fast_loads(raw)
                data = UnifiedCheckpointData.from_dict(raw_data)

                # Checksum verification
                if data.checksum:
                    self._verify_data_checksum(data)

                return data
        except CheckpointCorruptedError:
            raise
        except Exception as e:
            logger.warning(
                "kafka_redis_checkpoint.redis_load_failed",
                error=e,
            )

        # 2. Try File backup (Fallback)
        if self._file_backup:
            try:
                backup_data = self._file_backup.load(namespace)
                if backup_data:
                    logger.info(
                        "kafka_redis_checkpoint.loaded_file_backup",
                        namespace=namespace,
                    )

                    # Checksum verification
                    if backup_data.checksum:
                        self._verify_data_checksum(backup_data)

                    return backup_data
            except CheckpointCorruptedError:
                raise
            except Exception as e:
                logger.warning(
                    "kafka_redis_checkpoint.file_backup_load_failed",
                    error=e,
                )

        return None

    def _verify_data_checksum(self, data: UnifiedCheckpointData) -> None:
        """Data integrity verification."""
        if data.checksum is None:
            return  # checkpoint without checksum: skip verification (legacy entries)
        try:
            from baldur.audit.checksum import verify_checksum

            payload = {
                "wal_sequence": data.wal_sequence,
                "kafka_topic": data.kafka_topic,
                "kafka_partition": data.kafka_partition,
                "kafka_offset": data.kafka_offset,
                "timestamp": data.timestamp,
            }

            result = verify_checksum(payload, data.checksum, algorithm="crc32")

            if not result.is_valid:
                raise CheckpointCorruptedError(
                    "Kafka checkpoint checksum mismatch",
                    expected=result.expected,
                    computed=result.computed,
                )

        except ImportError:
            logger.debug("kafka_redis_checkpoint.checksum_module_unavailable")

    def commit(self, namespace: str) -> None:
        """Already saved to Redis."""
        pass

    def delete(self, namespace: str) -> bool:
        """Delete checkpoint (from both Redis + File)."""
        redis_result = False
        file_result = False

        try:
            key = self._get_key(namespace)
            redis_result = self._redis.delete(key) > 0
        except Exception:
            pass

        if self._file_backup:
            try:
                file_result = self._file_backup.delete(namespace)
            except Exception:
                pass

        return redis_result or file_result

    def exists(self, namespace: str) -> bool:
        """Check checkpoint existence (Redis or File)."""
        try:
            key = self._get_key(namespace)
            if self._redis.exists(key) > 0:
                return True
        except Exception:
            pass

        if self._file_backup:
            try:
                if self._file_backup.exists(namespace):
                    return True
            except Exception:
                pass

        return False
