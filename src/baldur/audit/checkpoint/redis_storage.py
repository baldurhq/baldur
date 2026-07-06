"""Redis-based checkpoint storage implementation.

Suitable for medium-scale distributed environments.
Features distributed locking, TTL expiration, and notification integration.

Version: 1.0.0
"""

from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING, Any

import structlog

from baldur.audit.checkpoint.strategy import (
    CheckpointCorruptedError,
    CheckpointError,
    CheckpointStorageStrategy,
    UnifiedCheckpointData,
)
from baldur.utils.serialization import fast_dumps_str, fast_loads

if TYPE_CHECKING:
    import redis

__all__ = [
    "RedisCheckpointStorage",
]

logger = structlog.get_logger()


class RedisCheckpointStorage(CheckpointStorageStrategy):
    """
    Redis-based checkpoint storage.

    Features:
    - Atomic storage in distributed environments
    - TTL-based auto-expiration (optional)
    - Distributed lock integration (multi-pod contention prevention)
    - Notification integration (on failure)
    """

    KEY_PREFIX = "baldur:checkpoint:"
    LOCK_KEY_PREFIX = "baldur:checkpoint:lock:"

    def __init__(
        self,
        redis_client: redis.Redis | None = None,
        ttl_seconds: int | None = None,
        use_distributed_lock: bool = True,
        lock_timeout_seconds: int = 5,
        enable_notification: bool = True,
    ):
        """
        Initialize Redis-based checkpoint storage.

        Args:
            redis_client: Redis client (optional, uses get_redis_client() fallback)
            ttl_seconds: Checkpoint TTL (None for unlimited)
            use_distributed_lock: Use distributed lock (required for multi-pod)
            lock_timeout_seconds: Distributed lock timeout
            enable_notification: Enable failure notifications

        Raises:
            ValueError: If redis_client is None and fallback is unavailable
        """
        super().__init__()
        if redis_client is None:
            from baldur.adapters.redis import get_redis_client

            redis_client = get_redis_client()
            if redis_client is None:
                raise ValueError("Redis client required but unavailable")
        # redis-py's stub returns Awaitable[X] | X dual unions for nearly every
        # sync command; widening to Any at the attribute keeps mypy out of every
        # sync call site (mirrors core/state_backend.py:RedisStateBackend._client).
        self._redis: Any = redis_client
        self._ttl = ttl_seconds
        self._use_distributed_lock = use_distributed_lock
        self._lock_timeout_seconds = lock_timeout_seconds
        self._enable_notification = enable_notification
        self._pending: dict[str, UnifiedCheckpointData] = {}
        self._local_lock = threading.Lock()

    def _get_key(self, namespace: str) -> str:
        """Generate Redis key."""
        return f"{self.KEY_PREFIX}{namespace}"

    def _get_lock_key(self, namespace: str) -> str:
        """Generate distributed lock key."""
        return f"{self.LOCK_KEY_PREFIX}{namespace}"

    def save(self, namespace: str, data: UnifiedCheckpointData) -> None:
        """
        Save checkpoint (distributed lock + notification integration).
        """
        try:
            if self._use_distributed_lock:
                self._save_with_lock(namespace, data)
            else:
                self._write_to_redis(namespace, data)
        except Exception as e:
            logger.exception(
                "redis_checkpoint.save_failed",
                error=e,
            )
            self._notify_failure(namespace, str(e))
            raise

    def _save_with_lock(self, namespace: str, data: UnifiedCheckpointData) -> None:
        """Save with distributed lock."""
        from datetime import timedelta

        try:
            from baldur_pro.services.coordination.distributed_recovery_lock import (
                DistributedRecoveryLock,
            )

            try:
                from baldur_pro.services.coordination.lock_backends import (
                    RedisLockBackend,
                )
            except ImportError:
                RedisLockBackend = None  # type: ignore[assignment,misc]

            lock = DistributedRecoveryLock(
                backends=[RedisLockBackend(self._redis)],
                lock_timeout=timedelta(seconds=self._lock_timeout_seconds),
            )

            session_id = f"checkpoint:{namespace}:{time.time()}"
            if lock.acquire(namespace, session_id, blocking=False):
                try:
                    self._write_to_redis(namespace, data)
                finally:
                    lock.release(namespace, session_id)
            else:
                raise CheckpointError(
                    f"Failed to acquire distributed lock for namespace: {namespace}"
                )

        except ImportError:
            # DistributedRecoveryLock not available, fall back to normal save
            logger.warning("redis_checkpoint.distributed_lock_unavailable")
            self._write_to_redis(namespace, data)

    def _write_to_redis(self, namespace: str, data: UnifiedCheckpointData) -> None:
        """Actually write to Redis."""
        key = self._get_key(namespace)
        value = fast_dumps_str(data.to_dict())

        if self._ttl:
            self._redis.setex(key, self._ttl, value)
        else:
            self._redis.set(key, value)

        logger.debug(
            "redis_checkpoint.saved",
            namespace=namespace,
            data=data.wal_sequence,
        )

    def _notify_failure(self, namespace: str, error: str) -> None:
        """Checkpoint save failure notification."""
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
                    title="Checkpoint Save Failed",
                    message=f"Redis checkpoint save failed for namespace '{namespace}': {error}",
                    priority=NotificationPriority.CRITICAL,
                    category=NotificationCategory.OPERATIONS,
                    source="checkpoint_storage",
                    metadata={
                        "namespace": namespace,
                        "storage_type": "redis",
                        "error": error,
                    },
                )
            )
            logger.info(
                "redis_checkpoint.failure_notification_sent",
                namespace=namespace,
            )

        except ImportError:
            logger.warning("redis_checkpoint.notification_manager_unavailable")
        except Exception as e:
            logger.warning(
                "redis_checkpoint.send_notification_failed",
                error=e,
            )

    def load(self, namespace: str) -> UnifiedCheckpointData | None:
        """Load checkpoint (includes checksum verification)."""
        key = self._get_key(namespace)
        raw = self._redis.get(key)

        if not raw:
            return None

        try:
            raw_data = fast_loads(raw)
            data = UnifiedCheckpointData.from_dict(raw_data)

            # Checksum verification (if present)
            if data.checksum:
                self._verify_data_checksum(data)

            return data
        except CheckpointCorruptedError:
            raise
        except Exception as e:
            logger.warning(
                "redis_checkpoint.load_failed",
                error=e,
            )
            return None

    def _verify_data_checksum(self, data: UnifiedCheckpointData) -> None:
        """Data integrity verification."""
        if data.checksum is None:
            return  # checkpoint without checksum: skip verification (legacy entries)
        try:
            from baldur.audit.checksum import verify_checksum

            # Verify checksum based on wal_sequence
            payload = {
                "wal_sequence": data.wal_sequence,
                "timestamp": data.timestamp,
                "version": data.version,
            }

            result = verify_checksum(payload, data.checksum, algorithm="crc32")

            if not result.is_valid:
                raise CheckpointCorruptedError(
                    "Checkpoint checksum mismatch",
                    expected=result.expected,
                    computed=result.computed,
                )

        except ImportError:
            logger.debug("redis_checkpoint.checksum_module_unavailable")

    def commit(self, namespace: str) -> None:
        """Commit pending checkpoint."""
        with self._local_lock:
            if namespace in self._pending:
                del self._pending[namespace]
        # Already saved in save()

    def delete(self, namespace: str) -> bool:
        """Delete checkpoint."""
        key = self._get_key(namespace)
        return self._redis.delete(key) > 0

    def exists(self, namespace: str) -> bool:
        """Check checkpoint existence."""
        key = self._get_key(namespace)
        return self._redis.exists(key) > 0
