"""
Local File-based Hash Chain Manager.

Contains:
- HashChainManager: Thread-safe manager for local hash chain state

416 D22: cross-process safety added via the existing
``audit/checkpoint/file_lock.py`` cross-platform lock primitive (POSIX
``fcntl.flock`` / Windows ``msvcrt.locking``). Multi-writer Gunicorn /
Celery deployments that share the same audit volume can now run safely
without Redis by setting ``use_file_lock=True`` (the default).
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import structlog

from baldur.audit.integrity.models import compute_hash
from baldur.core.file_utils import safe_unlink
from baldur.utils.serialization import fast_loads
from baldur.utils.time import utc_now

logger = structlog.get_logger()


class HashChainManager:
    """
    Manages hash chain state for audit logging.

    Thread-safe manager that maintains:
    - Current sequence number
    - Previous hash for chaining
    - Periodic checkpoints

    Multi-writer safety (D22):
        When ``use_file_lock=True`` and ``state_file`` is set, every
        ``add_integrity()`` call acquires an exclusive cross-process lock
        on a sibling ``.lock`` file before reading the latest state from
        disk and incrementing the sequence. This guarantees unique
        sequence numbers across multiple processes (Gunicorn workers,
        Celery worker, cron, etc.) sharing the same audit volume.
    """

    GENESIS_HASH = "GENESIS"

    def __init__(
        self,
        state_file: Path | None = None,
        use_file_lock: bool = True,
    ):
        """
        Initialize hash chain manager.

        Args:
            state_file: Optional path to persist chain state
            use_file_lock: Enable cross-process file lock (D22). Defaults
                to True. Set False only when (a) the deployment is
                verified single-writer, or (b) ``RedisHashChainManager``
                is layered on top and Redis serializes already.
        """
        self._lock = threading.RLock()
        self._use_file_lock = use_file_lock
        self._sequence = 0
        self._previous_hash = self.GENESIS_HASH
        self._state_file = state_file

        if state_file:
            self._load_state()

    def _load_state(self) -> None:
        """Load chain state from file."""
        if self._state_file and self._state_file.exists():
            try:
                data = fast_loads(self._state_file.read_text())
                self._sequence = data.get("sequence", 0)
                self._previous_hash = data.get("previous_hash", self.GENESIS_HASH)
                logger.debug(
                    "hash_chain.loaded_state",
                    sequence=self._sequence,
                )
            except Exception as e:
                logger.warning(
                    "hash_chain.load_state_failed",
                    error=e,
                )

    def _save_state(self) -> None:
        """Save chain state to file."""
        if self._state_file:
            try:
                self._state_file.parent.mkdir(parents=True, exist_ok=True)
                data = {
                    "sequence": self._sequence,
                    "previous_hash": self._previous_hash,
                    "updated_at": utc_now().isoformat(),
                }
                self._state_file.write_text(json.dumps(data, indent=2))
            except Exception as e:
                logger.warning(
                    "hash_chain.save_state_failed",
                    error=e,
                )

    @contextmanager
    def _locked_state_update(self) -> Iterator[None]:
        """Acquire exclusive cross-process lock on the state file (D22).

        Uses a sibling lock file (``.hash_chain_state.lock``) so the
        lock fd lifecycle is independent of the JSON state file's
        read/write cycle. The lock fd is released automatically on
        ``with`` exit (or process termination — ``fcntl.flock`` /
        ``msvcrt.locking`` releases on fd close).
        """
        from baldur.audit.checkpoint.file_lock import lock_file, unlock_file

        assert self._state_file is not None
        lock_path = self._state_file.with_suffix(".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(lock_path, "a+b") as fh:
            lock_file(fh, blocking=True)
            logger.debug("hash_chain.file_lock_acquired", path=str(lock_path))
            try:
                yield
            finally:
                unlock_file(fh)

    def _compute_and_persist(self, entry: dict[str, Any]) -> dict[str, Any]:
        """Increment sequence, compute hash, persist, and return enriched entry.

        Caller must hold both the in-process ``_lock`` and (when enabled)
        the cross-process file lock acquired via ``_locked_state_update()``.
        """
        self._sequence += 1

        # Add integrity info (without current_hash for now)
        entry["integrity"] = {
            "sequence": self._sequence,
            "previous_hash": self._previous_hash,
            "timestamp": utc_now().isoformat(),
        }

        # Compute hash of entry
        current_hash = compute_hash(entry)
        entry["integrity"]["current_hash"] = current_hash

        # Update state for next entry
        self._previous_hash = current_hash

        # Under multi-writer mode we MUST persist on every write — losing
        # the state file across restarts would corrupt the chain. Outside
        # the locked path we keep the historical "every 10 entries" cadence.
        if self._state_file and self._use_file_lock or self._sequence % 10 == 0:
            self._save_state()

        return entry

    def add_integrity(self, entry: dict[str, Any]) -> dict[str, Any]:
        """
        Add integrity fields to a log entry.

        Args:
            entry: Log entry dictionary

        Returns:
            Entry with integrity fields added
        """
        with self._lock:
            if self._state_file and self._use_file_lock:
                # Atomic load + increment + save under cross-process lock.
                with self._locked_state_update():
                    self._load_state()  # re-read latest from disk
                    return self._compute_and_persist(entry)
            return self._compute_and_persist(entry)

    def get_state(self) -> dict[str, Any]:
        """Get current chain state."""
        with self._lock:
            return {
                "sequence": self._sequence,
                "previous_hash": (
                    self._previous_hash[:16] + "..."
                    if len(self._previous_hash) > 16
                    else self._previous_hash
                ),
            }

    def reset(self) -> None:
        """Reset chain state (use with caution!)."""
        with self._lock:
            self._sequence = 0
            self._previous_hash = self.GENESIS_HASH
            if self._state_file:
                safe_unlink(self._state_file)
            logger.warning("hash_chain.chain_state_reset")


from baldur.utils.singleton import make_singleton_factory  # noqa: E402

get_hash_chain_manager, configure_hash_chain_manager, reset_hash_chain_manager = (
    make_singleton_factory("hash_chain_manager", HashChainManager)
)

__all__ = [
    "HashChainManager",
    "get_hash_chain_manager",
    "configure_hash_chain_manager",
    "reset_hash_chain_manager",
]
