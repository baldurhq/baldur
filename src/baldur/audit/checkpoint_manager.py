"""
CheckpointManager - persists the WAL processing sequence.

Stores the last processed WAL sequence on disk so it can be recovered after a
process restart. Used together with WriteAheadLog.recover_unprocessed() to
achieve 0% data loss.

Main features:
- Env-var-based path configuration (BALDUR_AUDIT_PATH)
- Multi-process file locking
- Write-permission verification with automatic fallback

Usage:
    from baldur.audit.checkpoint_manager import CheckpointManager

    checkpoint = CheckpointManager("/var/log/audit/checkpoint")

    # Save the checkpoint after processing completes
    checkpoint.save(last_seq=1234)

    # Load the checkpoint on restart
    last_seq = checkpoint.load()
    entries = wal.recover_unprocessed(last_seq)

Version: 1.1.0
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

import structlog

from baldur.core.serializable import SerializableMixin

logger = structlog.get_logger()


# =============================================================================
# Cross-Platform File Locking
# =============================================================================


def lock_file(f: BinaryIO) -> None:
    """Acquire a file lock (cross-platform)."""
    if sys.platform == "win32":
        import msvcrt

        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def unlock_file(f: BinaryIO) -> None:
    """Release a file lock (cross-platform)."""
    if sys.platform == "win32":
        import msvcrt

        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(f.fileno(), fcntl.LOCK_UN)


@dataclass(kw_only=True)
class CheckpointData(SerializableMixin):
    """Checkpoint data."""

    last_sequence: int = 0
    timestamp: float = 0.0
    version: int = 1


# CheckpointError: single source is checkpoint_strategy.py (Item 1 dedup)
from baldur.audit.checkpoint import CheckpointError  # noqa: F401


class CheckpointManager:
    """
    WAL processing sequence manager.

    .. deprecated::
        Use ``CheckpointStorageStrategy`` from ``checkpoint_strategy.py`` instead.
        This class will be removed in the next major version.

    Persists the last processed WAL sequence to disk so an exact recovery
    point is available after a process restart.
    """

    DEFAULT_CHECKPOINT_DIR = "/var/log/audit"
    DEFAULT_CHECKPOINT_FILENAME = "checkpoint.json"

    @staticmethod
    def _get_default_path() -> Path:
        """Resolve the default path from environment variables."""
        env_path = os.environ.get("BALDUR_AUDIT_PATH")
        if env_path:
            return Path(env_path) / "checkpoint.json"

        # Per-OS default path
        if os.name == "nt":  # Windows
            return Path(tempfile.gettempdir()) / "baldur" / "checkpoint.json"
        # Unix/Linux
        return Path("/var/log/audit") / "checkpoint.json"

    def __init__(
        self,
        checkpoint_path: str | Path | None = None,
        sync_on_write: bool = True,
    ):
        warnings.warn(
            "CheckpointManager is deprecated, use CheckpointStorageStrategy from "
            "baldur.audit.checkpoint_strategy instead",
            DeprecationWarning,
            stacklevel=2,
        )
        if checkpoint_path is None:
            checkpoint_path = self._get_default_path()

        self._path = Path(checkpoint_path)
        self._sync_on_write = sync_on_write
        self._lock = threading.RLock()

        # Permission check and fallback
        if not self._verify_write_permission():
            fallback_path = Path(tempfile.gettempdir()) / "baldur" / "checkpoint.json"
            logger.warning(
                "checkpoint_manager.no_write_permission_falling",
                path=self._path,
                fallback_path=fallback_path,
            )
            self._path = fallback_path

        # Create the directory
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def _verify_write_permission(self) -> bool:
        """Verify write permission."""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            test_file = self._path.parent / ".write_test"
            test_file.touch()
            test_file.unlink()
            return True
        except (PermissionError, OSError):
            return False

    @property
    def path(self) -> Path:
        """Checkpoint file path."""
        return self._path

    def save(self, last_sequence: int) -> None:
        """
        Save the checkpoint (with multi-process file locking).

        Writes to a temporary file first and renames, for atomicity.

        Args:
            last_sequence: Last processed sequence number

        Raises:
            CheckpointError: When saving fails
        """
        with self._lock:
            checkpoint_data = CheckpointData(
                last_sequence=last_sequence,
                timestamp=time.time(),
            )

            temp_path = self._path.with_suffix(".tmp")
            lock_file_path = self._path.with_suffix(".lock")

            try:
                # Acquire the file lock
                with open(lock_file_path, "wb") as lock_f:
                    try:
                        lock_file(lock_f)

                        # Write to the temporary file
                        with open(temp_path, "w", encoding="utf-8") as f:
                            json.dump(checkpoint_data.to_dict(), f, indent=2)

                            if self._sync_on_write:
                                f.flush()
                                os.fsync(f.fileno())

                        # Atomic rename
                        temp_path.replace(self._path)

                        # Directory fsync (optional, recommended on Linux).
                        # os.O_DIRECTORY is POSIX-only — Windows mypy and
                        # runtime both lack it; the AttributeError catch
                        # below preserves the cross-platform fail-safe.
                        if self._sync_on_write:
                            try:
                                dir_fd = os.open(
                                    str(self._path.parent),
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
                    "checkpoint.saved",
                    last_sequence=last_sequence,
                )

            except (BlockingIOError, OSError) as e:
                # Another process holds the lock - skip
                logger.warning(
                    "checkpoint_manager.lock_contention_skipping_save",
                    error=e,
                )

            except Exception as e:
                # Clean up the temporary file
                try:
                    temp_path.unlink(missing_ok=True)
                except Exception:
                    pass

                raise CheckpointError(f"Failed to save checkpoint: {e}") from e

    def load(self) -> int:
        """
        Load the checkpoint.

        Returns 0 when the file is missing or unreadable.

        Returns:
            Last processed sequence number (0 if none)
        """
        with self._lock:
            if not self._path.exists():
                logger.debug("audit_checkpoint.cache_hit")
                return 0

            try:
                with open(self._path, encoding="utf-8") as f:
                    data = json.load(f)

                checkpoint_data = CheckpointData.from_dict(data)
                logger.debug(
                    "checkpoint.loaded",
                    checkpoint_data=checkpoint_data.last_sequence,
                )
                return checkpoint_data.last_sequence

            except Exception as e:
                logger.warning(
                    "audit_checkpoint.load_checkpoint_failed",
                    error=e,
                )
                return 0

    def load_full(self) -> CheckpointData | None:
        """
        Load the full checkpoint data.

        Returns:
            CheckpointData or None
        """
        with self._lock:
            if not self._path.exists():
                return None

            try:
                with open(self._path, encoding="utf-8") as f:
                    data = json.load(f)

                return CheckpointData.from_dict(data)

            except Exception:
                return None

    def exists(self) -> bool:
        """Whether the checkpoint file exists."""
        return self._path.exists()

    def delete(self) -> bool:
        """
        Delete the checkpoint file.

        Returns:
            Whether deletion succeeded
        """
        with self._lock:
            try:
                self._path.unlink(missing_ok=True)
                return True
            except Exception:
                return False

    def get_age_seconds(self) -> float | None:
        """
        Checkpoint age in seconds.

        Returns:
            Elapsed time since the last save, or None
        """
        checkpoint_data = self.load_full()
        if checkpoint_data is None:
            return None

        return time.time() - checkpoint_data.timestamp


# =============================================================================
# Singleton Pattern
# =============================================================================

from baldur.utils.singleton import make_singleton_factory  # noqa: E402

_get_checkpoint_manager, configure_checkpoint_manager, reset_checkpoint_manager = (
    make_singleton_factory("checkpoint_manager", CheckpointManager)
)


def get_checkpoint_manager(
    checkpoint_path: str | Path | None = None,
) -> CheckpointManager:
    """
    Return default CheckpointManager instance.

    .. deprecated::
        Use ``get_default_checkpoint_strategy()`` from ``checkpoint_strategy.py`` instead.
    """
    warnings.warn(
        "get_checkpoint_manager() is deprecated, use "
        "get_default_checkpoint_strategy() from baldur.audit.checkpoint_strategy instead",
        DeprecationWarning,
        stacklevel=2,
    )
    return _get_checkpoint_manager()
