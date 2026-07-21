"""File-based checkpoint storage implementation.

Pure Python, no external dependencies. Suitable for small-scale deployments.
Features multi-process file locking, fsync durability, and atomic writes.

Version: 1.0.0
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import datetime
from pathlib import Path

import structlog

from baldur.audit.checkpoint.file_lock import lock_file, unlock_file
from baldur.audit.checkpoint.strategy import (
    CheckpointError,
    CheckpointStorageStrategy,
    UnifiedCheckpointData,
    get_load_failures_counter,
    get_save_failures_counter,
)
from baldur.utils.fs import ResolvedDir, resolve_writable_dir
from baldur.utils.time import utc_now

__all__ = [
    "FileCheckpointStorage",
]

logger = structlog.get_logger()


class FileCheckpointStorage(CheckpointStorageStrategy):
    """
    File-based checkpoint storage.

    Features:
    - Pure Python, no external dependencies
    - Multi-process file locking support
    - fsync disk durability guarantee
    - Atomic writes (temp file + rename)
    """

    DEFAULT_DIR = "/var/log/audit"
    DEFAULT_FILENAME = "checkpoint.json"
    PATH_ENV_VAR = "BALDUR_AUDIT_PATH"

    def __init__(
        self,
        base_path: str | Path | None = None,
        sync_on_write: bool = True,
        base_path_operator_set: bool | None = None,
        purpose: str = "checkpoint",
    ):
        """
        Initialize file-based checkpoint storage.

        Args:
            base_path: Base path for checkpoint files
            sync_on_write: Whether to perform fsync on write
            base_path_operator_set: Whether ``base_path`` came from operator
                input. ``None`` infers it: an explicit ``base_path`` or a set
                ``BALDUR_AUDIT_PATH`` means the operator chose the directory,
                so an unwritable one raises instead of falling back.
            purpose: Resolution identifier, distinguishing this store's
                fallback directory from a sibling store's.

        Raises:
            ConfigurationError: When an operator-chosen base path is not
                writable, or when no fallback directory is writable.
        """
        requested_path = self._get_base_path(base_path)
        if base_path_operator_set is None:
            base_path_operator_set = bool(base_path) or bool(
                os.environ.get(self.PATH_ENV_VAR)
            )

        self._resolved_dir = resolve_writable_dir(
            requested_path,
            purpose=purpose,
            operator_set=base_path_operator_set,
            env_override_name=self.PATH_ENV_VAR,
        )
        self._base_path = self._resolved_dir.path
        self._sync_on_write = sync_on_write
        self._lock = threading.RLock()

    @property
    def base_path(self) -> Path:
        """Directory checkpoint files are written to (post-resolution)."""
        return self._base_path

    @property
    def resolved_dir(self) -> ResolvedDir:
        """Full directory-resolution outcome, including any fallback."""
        return self._resolved_dir

    def _get_base_path(self, base_path: str | Path | None) -> Path:
        """Determine base path."""
        if base_path:
            return Path(base_path)

        env_path = os.environ.get(self.PATH_ENV_VAR)
        if env_path:
            return Path(env_path)

        if os.name == "nt":  # Windows
            return Path(tempfile.gettempdir()) / "baldur"
        return Path(self.DEFAULT_DIR)

    def _get_file_path(self, namespace: str) -> Path:
        """Get namespace-specific file path."""
        return self._base_path / f"checkpoint.{namespace}.json"

    def save(self, namespace: str, data: UnifiedCheckpointData) -> None:
        """Save checkpoint (atomic, cross-process file lock protected)."""
        with self._lock:
            file_path = self._get_file_path(namespace)
            tmp_path = file_path.with_suffix(".tmp")
            lock_file_path = file_path.with_suffix(".lock")

            try:
                with open(lock_file_path, "wb") as lock_f:
                    try:
                        lock_file(lock_f)

                        with open(tmp_path, "w", encoding="utf-8") as f:
                            json.dump(data.to_dict(), f, indent=2)

                            if self._sync_on_write:
                                f.flush()
                                os.fsync(f.fileno())

                        # Atomic rename
                        tmp_path.replace(file_path)

                        # Directory fsync (Linux)
                        if self._sync_on_write:
                            try:
                                # os.O_DIRECTORY is POSIX-only; Windows mypy
                                # doesn't see it. Runtime is guarded by the
                                # AttributeError catch below.
                                dir_fd = os.open(
                                    str(file_path.parent),
                                    os.O_RDONLY | os.O_DIRECTORY,  # type: ignore[attr-defined]
                                )
                                try:
                                    os.fsync(dir_fd)
                                finally:
                                    os.close(dir_fd)
                            except (OSError, AttributeError):
                                pass

                    finally:
                        try:
                            unlock_file(lock_f)
                        except Exception:
                            pass

                logger.debug(
                    "file_checkpoint.saved",
                    namespace=namespace,
                    data=data.wal_sequence,
                )

            except Exception as e:
                try:
                    tmp_path.unlink(missing_ok=True)
                except Exception:
                    pass
                counter = get_save_failures_counter()
                if counter:
                    counter.labels(storage_type="file").inc()
                raise CheckpointError(f"Failed to save checkpoint: {e}") from e

    def _migrate_legacy_file(self, namespace: str) -> None:
        """Legacy checkpoint.json -> checkpoint.{namespace}.json migration (file lock protected)."""
        if namespace != "default":
            return

        legacy_path = self._base_path / "checkpoint.json"
        target_path = self._get_file_path(namespace)

        if not legacy_path.exists() or target_path.exists():
            return

        lock_file_path = legacy_path.with_suffix(".lock")
        try:
            with open(lock_file_path, "wb") as lock_f:
                try:
                    lock_file(lock_f, blocking=False)

                    # double-check after lock
                    if not legacy_path.exists() or target_path.exists():
                        return

                    legacy_path.rename(target_path)
                    logger.info(
                        "file_checkpoint.legacy_migrated",
                        legacy_path=str(legacy_path),
                        target_path=str(target_path),
                    )

                finally:
                    try:
                        unlock_file(lock_f)
                    except Exception:
                        pass
        except (BlockingIOError, OSError):
            pass
        except Exception as e:
            logger.warning(
                "file_checkpoint.legacy_migration_failed",
                error=e,
            )

    def load(self, namespace: str) -> UnifiedCheckpointData | None:
        """Load checkpoint (includes legacy migration)."""
        with self._lock:
            # Legacy checkpoint.json -> checkpoint.default.json migration
            self._migrate_legacy_file(namespace)

            file_path = self._get_file_path(namespace)
            if not file_path.exists():
                return None

            try:
                with open(file_path, encoding="utf-8") as f:
                    raw_data = json.load(f)

                # Legacy format conversion + write-back
                if "last_sequence" in raw_data and "wal_sequence" not in raw_data:
                    data = UnifiedCheckpointData.from_legacy_checkpoint_data(raw_data)
                    try:
                        self.save(namespace, data)
                    except Exception:
                        pass
                    return data

                return UnifiedCheckpointData.from_dict(raw_data)

            except Exception as e:
                logger.warning(
                    "file_checkpoint.load_failed",
                    error=e,
                )
                counter = get_load_failures_counter()
                if counter:
                    counter.labels(storage_type="file").inc()
                return None

    def commit(self, namespace: str) -> None:
        """File storage is already committed in save()."""
        pass  # No-op for file storage

    def delete(self, namespace: str) -> bool:
        """Delete checkpoint."""
        with self._lock:
            file_path = self._get_file_path(namespace)
            try:
                file_path.unlink(missing_ok=True)
                return True
            except Exception:
                return False

    def exists(self, namespace: str) -> bool:
        """Check checkpoint existence."""
        return self._get_file_path(namespace).exists()

    def get_age_seconds(self, namespace: str) -> float | None:
        """
        Get checkpoint age in seconds.

        Returns:
            Seconds since last save, or None
        """
        data = self.load(namespace)
        if data is None:
            return None

        try:
            ts = datetime.fromisoformat(data.timestamp)
            return (utc_now() - ts).total_seconds()
        except (ValueError, TypeError):
            return None
