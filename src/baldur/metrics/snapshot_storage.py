"""
L1 Metric Snapshot Storage.

Provides local file-based snapshot storage for metric values.
This is the "last resort" fallback when all other data sources fail.

Design Philosophy:
- "Last Known Good" (LKG) pattern
- Atomic writes using Write-to-Temp-and-Rename
- Non-blocking: failures don't affect system operation
- Age tracking for data freshness indication

Fallback Hierarchy:
1. Real-time Push Events
2. DB Query (Manual Sync)
3. Redis Air-Gap
4. L1 Local Snapshot ← This module
5. Safe Defaults (Emergency)
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

from baldur.core.file_utils import safe_unlink
from baldur.core.serializable import SerializableMixin

logger = structlog.get_logger()


def _get_snapshot_max_age() -> int:
    """Read snapshot_max_age from MetricsSettings via the layered provider.

    Layered read (686 D1/D5) so a console edit of the metrics domain takes
    effect; env base when no RuntimeConfigManager is registered.
    """
    try:
        from baldur.settings.layered_provider import get_layered_settings
        from baldur.settings.metrics import MetricsSettings

        return get_layered_settings(MetricsSettings, "metrics").snapshot_max_age
    except Exception:
        return 3600  # 1 hour fallback


@dataclass
class MetricSnapshot(SerializableMixin):
    """Metric snapshot data."""

    # Metric values (per domain)
    values: dict[str, dict[str, Any]] = field(default_factory=dict)

    # Metadata
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    version: str = "1.0"
    source: str = "unknown"

    @property
    def age_seconds(self) -> float:
        """Snapshot age in seconds."""
        return time.time() - self.updated_at

    def get_value(self, category: str, key: str, default: Any = None) -> Any:
        """
        Read a value from the snapshot.

        Args:
            category: Category (e.g. "dlq_pending", "circuit_breaker")
            key: Key (e.g. "payment", "toss")
            default: Default value

        Returns:
            Stored value, or the default
        """
        if category not in self.values:
            return default
        return self.values[category].get(key, default)

    def set_value(self, category: str, key: str, value: Any) -> None:
        """
        Store a value in the snapshot.

        Args:
            category: Category
            key: Key
            value: Value
        """
        if category not in self.values:
            self.values[category] = {}
        self.values[category][key] = value
        self.updated_at = time.time()


class MetricSnapshotStorage:
    """
    L1 local-file snapshot storage.

    Periodically persists metric values to a local file so the "last known
    value" stays available even when every data source fails.

    Features:
    - Atomic writes: Write-to-Temp-and-Rename pattern
    - Non-blocking: failures don't affect system operation
    - Age tracking: tracks snapshot age
    - Thread-safe: safe for concurrent access

    Example:
        >>> storage = MetricSnapshotStorage("/var/lib/baldur/metrics")
        >>>
        >>> # Save a snapshot value
        >>> storage.save_value("dlq_pending", "payment", 5)
        >>>
        >>> # Load a snapshot value
        >>> value = storage.load_value("dlq_pending", "payment")
        >>> age = storage.get_snapshot_age()
    """

    DEFAULT_FILENAME = "last_known_metrics.json"
    DEFAULT_MAX_AGE = 3600  # Legacy constant kept for backward compatibility

    def __init__(
        self,
        storage_dir: str | None = None,
        filename: str = DEFAULT_FILENAME,
        max_age_seconds: float | None = None,
    ):
        """
        Initialize MetricSnapshotStorage.

        Args:
            storage_dir: Storage directory (default location when None)
            filename: Snapshot file name
            max_age_seconds: Max snapshot validity in seconds. Read from
                Settings when None.
        """
        self._storage_dir = (
            Path(storage_dir) if storage_dir else self._get_default_dir()
        )
        self._filename = filename
        self._max_age = (
            max_age_seconds if max_age_seconds is not None else _get_snapshot_max_age()
        )
        self._lock = threading.Lock()
        self._snapshot: MetricSnapshot | None = None
        self._dirty = False

        # Create the directory
        self._ensure_directory()

        # Load any existing snapshot
        self._load_snapshot()

    @property
    def file_path(self) -> Path:
        """Snapshot file path."""
        return self._storage_dir / self._filename

    @property
    def snapshot(self) -> MetricSnapshot | None:
        """Current snapshot."""
        return self._snapshot

    def _get_default_dir(self) -> Path:
        """Default storage directory."""
        try:
            from baldur.settings.metrics import get_metrics_settings

            snapshot_dir = get_metrics_settings().snapshot_dir
            if snapshot_dir:
                return Path(snapshot_dir)
        except Exception:
            pass

        # Default: under the current directory
        return Path.cwd() / ".baldur"

    def _ensure_directory(self) -> None:
        """Ensure the directory exists, creating it if needed."""
        try:
            self._storage_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            logger.warning(
                "snapshot.create_directory_failed",
                storage_dir=self._storage_dir,
                error=e,
            )

    def _load_snapshot(self) -> None:
        """Load the snapshot from file."""
        try:
            if not self.file_path.exists():
                self._snapshot = MetricSnapshot(source="new")
                logger.debug("snapshot_storage.initialized")
                return

            with open(self.file_path, encoding="utf-8") as f:
                data = json.load(f)

            self._snapshot = MetricSnapshot.from_dict(data)
            logger.info(
                "snapshot.loaded_snapshot_age_categories",
                snapshot_age_seconds=self._snapshot.age_seconds,
                values_count=len(self._snapshot.values),
            )
        except Exception as e:
            logger.warning(
                "snapshot.load_snapshot_failed",
                error=e,
            )
            self._snapshot = MetricSnapshot(source="new_after_error")

    def _save_snapshot_atomic(self) -> bool:
        """
        Save the snapshot atomically.

        Uses the Write-to-Temp-and-Rename pattern:
        1. Write to a temp file
        2. fsync to flush to disk
        3. Rename onto the real filename (atomic operation)

        Returns:
            Whether the save succeeded
        """
        if self._snapshot is None:
            return False

        try:
            # Create the temp file (in the same directory)
            fd, temp_path = tempfile.mkstemp(
                suffix=".tmp",
                prefix="snapshot_",
                dir=self._storage_dir,
            )

            try:
                # Write JSON
                data = self._snapshot.to_dict()
                json_str = json.dumps(data, indent=2, ensure_ascii=False)

                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(json_str)
                    f.flush()
                    os.fsync(f.fileno())

                # Atomic rename
                os.replace(temp_path, self.file_path)

                self._dirty = False
                logger.debug(
                    "snapshot.saved_snapshot",
                    file_path=self.file_path,
                )
                return True

            except Exception:
                # Clean up the temp file
                safe_unlink(Path(temp_path))
                raise

        except Exception as e:
            logger.warning(
                "snapshot.save_snapshot_failed",
                error=e,
            )
            return False

    def save_value(
        self,
        category: str,
        key: str,
        value: Any,
        immediate: bool = False,
    ) -> bool:
        """
        Store a value.

        Args:
            category: Category (e.g. "dlq_pending", "circuit_breaker")
            key: Key (e.g. "payment", "toss")
            value: Value
            immediate: Whether to persist to disk right away

        Returns:
            Whether the operation succeeded
        """
        with self._lock:
            if self._snapshot is None:
                self._snapshot = MetricSnapshot(source="save")

            self._snapshot.set_value(category, key, value)
            self._dirty = True

            if immediate:
                return self._save_snapshot_atomic()
            return True

    def save_bulk(
        self,
        values: dict[str, dict[str, Any]],
        source: str = "bulk",
    ) -> bool:
        """
        Store several values at once.

        Args:
            values: Dict of the form {category: {key: value}}
            source: Identifier of the storing source

        Returns:
            Whether the operation succeeded
        """
        with self._lock:
            if self._snapshot is None:
                self._snapshot = MetricSnapshot(source=source)

            for category, items in values.items():
                for key, value in items.items():
                    self._snapshot.set_value(category, key, value)

            self._snapshot.source = source
            self._dirty = True
            return self._save_snapshot_atomic()

    def load_value(
        self,
        category: str,
        key: str,
        default: Any = None,
        max_age: float | None = None,
    ) -> Any:
        """
        Load a value.

        Args:
            category: Category
            key: Key
            default: Default value
            max_age: Max acceptable age in seconds; the default is used
                when None

        Returns:
            Stored value, or the default (also when the snapshot is too old)
        """
        with self._lock:
            if self._snapshot is None:
                return default

            effective_max_age = max_age if max_age is not None else self._max_age

            if self._snapshot.age_seconds > effective_max_age:
                logger.debug(
                    "snapshot.value_too_old",
                    snapshot_age_seconds=self._snapshot.age_seconds,
                    effective_max_age=effective_max_age,
                )
                return default

            return self._snapshot.get_value(category, key, default)

    def load_all(self, max_age: float | None = None) -> dict[str, dict[str, Any]]:
        """
        Load every value.

        Args:
            max_age: Max acceptable age in seconds

        Returns:
            All stored values, or an empty dict
        """
        with self._lock:
            if self._snapshot is None:
                return {}

            effective_max_age = max_age if max_age is not None else self._max_age

            if self._snapshot.age_seconds > effective_max_age:
                return {}

            return dict(self._snapshot.values)

    def get_snapshot_age(self) -> float | None:
        """
        Read the snapshot age.

        Returns:
            Snapshot age in seconds, or None
        """
        with self._lock:
            if self._snapshot is None:
                return None
            return self._snapshot.age_seconds

    def get_snapshot_info(self) -> dict[str, Any]:
        """
        Read snapshot info.

        Returns:
            Snapshot metadata
        """
        with self._lock:
            if self._snapshot is None:
                return {"exists": False}

            return {
                "exists": True,
                "age_seconds": self._snapshot.age_seconds,
                "created_at": self._snapshot.created_at,
                "updated_at": self._snapshot.updated_at,
                "source": self._snapshot.source,
                "categories": list(self._snapshot.values.keys()),
                "is_valid": self._snapshot.age_seconds <= self._max_age,
                "file_path": str(self.file_path),
            }

    def flush(self) -> bool:
        """
        Persist pending changes to disk.

        Returns:
            Whether the operation succeeded
        """
        with self._lock:
            if not self._dirty:
                return True
            return self._save_snapshot_atomic()

    def clear(self) -> bool:
        """
        Reset the snapshot.

        Returns:
            Whether the operation succeeded
        """
        with self._lock:
            self._snapshot = MetricSnapshot(source="cleared")
            self._dirty = True
            return self._save_snapshot_atomic()


# =============================================================================
# Singleton Instance
# =============================================================================

from baldur.utils.singleton import make_singleton_factory

get_snapshot_storage, configure_snapshot_storage, reset_snapshot_storage = (
    make_singleton_factory("snapshot_storage", MetricSnapshotStorage)
)


__all__ = [
    "MetricSnapshot",
    "MetricSnapshotStorage",
    "configure_snapshot_storage",
    "get_snapshot_storage",
    "reset_snapshot_storage",
]
