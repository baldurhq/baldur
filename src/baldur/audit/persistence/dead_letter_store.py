"""Dead letter store for poison-pill entry isolation.

Entries that repeatedly fail during flush are moved to a dedicated
LMDB database so that an operator can review and replay them.
"""

from __future__ import annotations

import struct
from typing import Any

import structlog

from baldur.audit.persistence.checksum import compute_checksum
from baldur.audit.persistence.config import DiskBufferSettings
from baldur.audit.persistence.disk_buffer_models import BufferEntry
from baldur.utils.serialization import fast_dumps_str, fast_loads
from baldur.utils.time import utc_now

__all__ = [
    "DeadLetterStore",
]

logger = structlog.get_logger()


class DeadLetterStore:
    """Manages dead-letter entries within the LMDB environment.

    Poison-pill entries (those exceeding ``max_flush_retries``) are
    moved here so they stop blocking the normal flush pipeline.

    Args:
        env: LMDB environment handle.
        dead_letter_db: LMDB dead-letter database handle (may be ``None``
            when the feature is disabled).
        settings: Disk buffer settings.
        stats: Mutable stats dict shared with the parent buffer.
    """

    def __init__(
        self,
        *,
        env: Any,
        dead_letter_db: Any,
        settings: DiskBufferSettings,
        stats: dict[str, int],
    ) -> None:
        self._env = env
        self._dead_letter_db = dead_letter_db
        self._settings = settings
        self._stats = stats

        # Poison-pill retry counters  (key_str -> count)
        self._retry_counters: dict[str, int] = {}

    # ── Retry counter management ──────────────────────────

    def increment_retry_counter(self, key: bytes) -> None:
        """Increment the retry counter for *key*."""
        key_str = key.decode()
        self._retry_counters[key_str] = self._retry_counters.get(key_str, 0) + 1

    def clear_retry_counters(self, keys: list[bytes]) -> None:
        """Remove retry counters for successfully flushed *keys*."""
        for key in keys:
            key_str = key.decode()
            self._retry_counters.pop(key_str, None)

    def is_poison_pill(self, key: bytes) -> bool:
        """Return ``True`` if *key* has exceeded ``max_flush_retries``."""
        key_str = key.decode()
        return self._retry_counters.get(key_str, 0) >= self._settings.max_flush_retries

    # ── Move / alert ──────────────────────────────────────

    def move(
        self,
        entry: BufferEntry,
        *,
        delete_fn: Any,
        error: str = "",
    ) -> None:
        """Move a poison-pill entry to the dead-letter database.

        Args:
            entry: The entry to isolate.
            delete_fn: Callable to delete the entry from the main DB
                (accepts ``key: bytes`` and returns ``bool``).
            error: Optional failure reason string.
        """
        if not self._settings.enable_dead_letter_db:
            logger.warning(
                "disk_buffer.poison_pill_detected_dlq",
                entry=entry.key,
            )
            delete_fn(entry.key)  # Prevent infinite loop
            return

        try:
            dlq_entry = {
                **entry.data,
                "_dead_letter_at": utc_now().isoformat(),
                "_original_key": entry.key.decode(),
                "_retry_count": self._retry_counters.get(entry.key.decode(), 0),
                "_failure_reason": error,
                "status": "requires_review",
            }

            dlq_data = fast_dumps_str(dlq_entry, default=str)
            dlq_bytes = dlq_data.encode("utf-8")
            checksum = compute_checksum(dlq_bytes)
            dlq_value = struct.pack(">I", checksum) + dlq_bytes

            with self._env.begin(write=True, db=self._dead_letter_db) as txn:
                txn.put(entry.key, dlq_value)

            # Remove from the main entries database
            delete_fn(entry.key)

            # Clean up retry counter
            self._retry_counters.pop(entry.key.decode(), None)

            self._stats["dead_letter_moves"] += 1
            logger.warning(
                "disk_buffer.moved_dead_letter_db",
                entry=entry.key.decode(),
            )

            # Send alert
            self._send_alert(entry)

        except Exception as e:
            logger.exception(
                "disk_buffer.dead_letter_move_failed",
                error=e,
            )

    def _send_alert(self, entry: BufferEntry) -> None:
        """Send a poison-pill notification."""
        try:
            from baldur_pro.services.unified_notification import (
                NotificationCategory,
                NotificationPayload,
                NotificationPriority,
                UnifiedNotificationManager,
            )

            payload = NotificationPayload(
                title="\u26a0\ufe0f DiskBuffer Poison Pill Detected",
                message=(
                    f"Repeatedly failing entry isolated to Dead Letter DB: "
                    f"{entry.key.decode()}"
                ),
                priority=NotificationPriority.HIGH,
                category=NotificationCategory.OPERATIONS,
                source="DiskPersistentBuffer",
                dedup_key=f"disk_buffer:poison_pill:{entry.key.decode()[:20]}",
            )
            UnifiedNotificationManager().notify(payload)
        except Exception as e:
            logger.debug(
                "disk_buffer.alert_send_failed",
                error=e,
            )

    # ── Query / replay ────────────────────────────────────

    def count(self) -> int:
        """Return the number of dead-letter entries."""
        if not self._settings.enable_dead_letter_db or self._dead_letter_db is None:
            return 0
        with self._env.begin(db=self._dead_letter_db) as txn:
            return txn.stat()["entries"]

    def get_dead_letters(self, limit: int = 100) -> list[dict[str, Any]]:
        """Retrieve dead-letter entries for operator review.

        Args:
            limit: Maximum number of entries to return.

        Returns:
            List of dead-letter entry dicts.
        """
        if not self._settings.enable_dead_letter_db or self._dead_letter_db is None:
            return []

        result: list[dict[str, Any]] = []
        with self._env.begin(db=self._dead_letter_db) as txn:
            cursor = txn.cursor()
            count = 0
            for key, value in cursor:
                if count >= limit:
                    break
                try:
                    data_bytes = value[4:]  # skip checksum
                    data = fast_loads(data_bytes)
                    result.append({"key": key.decode(), **data})
                    count += 1
                except Exception as e:
                    logger.warning(
                        "disk_buffer.dead_letter_parse_failed",
                        key=key.decode(errors="replace"),
                        error=str(e),
                    )
        return result

    def replay(
        self,
        key: bytes,
        *,
        put_fn: Any,
    ) -> bool:
        """Replay a dead-letter entry back into the main buffer.

        Args:
            key: Dead-letter entry key.
            put_fn: Callable to insert data back into the main buffer
                (accepts ``entry: dict`` and returns ``bytes | None``).

        Returns:
            ``True`` on success, ``False`` otherwise.
        """
        if not self._settings.enable_dead_letter_db or self._dead_letter_db is None:
            return False

        with self._env.begin(db=self._dead_letter_db) as txn:
            value = txn.get(key)
            if not value:
                return False

        try:
            data_bytes = value[4:]
            data = fast_loads(data_bytes)

            # Strip dead-letter metadata before re-inserting
            data.pop("_dead_letter_at", None)
            data.pop("_retry_count", None)
            data.pop("_failure_reason", None)
            data.pop("status", None)

            put_fn(data)

            # Remove from dead-letter DB
            with self._env.begin(write=True, db=self._dead_letter_db) as txn:
                txn.delete(key)

            logger.info(
                "disk_buffer.replayed_dead_letter",
                buffer_entry_key=key.decode(),
            )
            return True
        except Exception as e:
            logger.exception(
                "disk_buffer.dead_letter_replay_failed",
                error=e,
            )
            return False
