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
import os
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from baldur.audit.integrity.models import (
    compute_hash,
    record_source_reset,
    sanitize_integrity_annotations,
)
from baldur.core.exceptions import HashChainSequenceRefusedError
from baldur.core.file_utils import safe_unlink
from baldur.utils.serialization import fast_loads
from baldur.utils.time import utc_now

if TYPE_CHECKING:
    from baldur.audit.integrity.ledger_tail import LedgerTailReader

logger = structlog.get_logger()


class HashChainManager:
    """
    Manages hash chain state for audit logging.

    Thread-safe manager that maintains:
    - Current sequence number
    - Previous hash for chaining
    - Periodic checkpoints

    Multi-writer safety:
        When ``use_file_lock=True`` and ``state_file`` is set, every
        ``add_integrity()`` call acquires an exclusive cross-process lock
        on a sibling ``.lock`` file before reading the latest state from
        disk and incrementing the sequence. This guarantees unique
        sequence numbers across multiple processes (Gunicorn workers,
        Celery worker, cron, etc.) sharing the same audit volume.

        The cross-process half of that guarantee is pinned by the
        multiprocess contention case in
        ``tests/unit/audit/integrity/test_hash_chain_file_lock.py``, which
        drives one shared state file from separate processes and fails when
        the lock is off. The thread-based case in the same file covers the
        in-process lock only — that one passes with the file lock removed.

    Sequence-source recovery:
        The state file is the sequence source and the ledger is what it
        orders; when the two disagree the ledger wins. With a ledger reader
        wired, every write compares the number about to be minted against the
        highest sequence the ledger already holds and re-anchors to that tail
        when the source is behind it — the state file having been removed,
        truncated by an unclean kill, restored from a backup, or (for a
        distributed chain's fallback manager) simply never written since the
        last outage. The entry that continues the chain carries the repair
        under its own hash.
    """

    GENESIS_HASH = "GENESIS"

    def __init__(
        self,
        state_file: Path | None = None,
        use_file_lock: bool = True,
        ledger: LedgerTailReader | None = None,
    ):
        """
        Initialize hash chain manager.

        Args:
            state_file: Optional path to persist chain state
            use_file_lock: Enable cross-process file lock (D22). Defaults
                to True. Set False only when (a) the deployment is
                verified single-writer, or (b) ``RedisHashChainManager``
                is layered on top and Redis serializes already.
            ledger: Reader for the ledger this chain's entries land in. The
                adapter that owns the files supplies it; ``None`` leaves the
                recovery inert, which is what every construction site that
                does not own a ledger wants.
        """
        self._lock = threading.RLock()
        self._use_file_lock = use_file_lock
        self._sequence = 0
        self._previous_hash = self.GENESIS_HASH
        self._state_file = state_file
        self._ledger = ledger
        self._state_loaded = False

        if state_file:
            # Serialized with the saves: ``os.replace`` fails on Windows while
            # any process holds the target open, so a sibling recycling under
            # ``max_requests`` must not read this file outside the lock.
            if use_file_lock:
                with self._locked_state_update():
                    self._load_state()
            else:
                self._load_state()

    def _load_state(self) -> bool:
        """Load chain state from file.

        Returns:
            ``True`` when a state file existed and parsed. On ``False`` the
            in-process sequence and previous hash are left exactly as they
            were — the caller decides what an unreadable source means, which
            with a wired ledger is "re-derive from the ledger's own tail".
        """
        if not (self._state_file and self._state_file.exists()):
            self._state_loaded = False
            return False

        try:
            data = fast_loads(self._state_file.read_text())
            self._sequence = data.get("sequence", 0)
            self._previous_hash = data.get("previous_hash", self.GENESIS_HASH)
            logger.debug(
                "hash_chain.loaded_state",
                sequence=self._sequence,
            )
            self._state_loaded = True
            return True
        except Exception as e:
            logger.warning(
                "hash_chain.load_state_failed",
                error=e,
            )
            self._state_loaded = False
            return False

    def _save_state(self) -> None:
        """Save chain state to file, replacing it atomically.

        The temp name carries this writer's pid: a fixed shared name would let
        two processes truncate each other's half-written temp. No ``fsync`` —
        the ledger this file indexes is flushed but never fsynced, so an
        fsynced state file would out-durable it at the cost of a disk sync on
        every audited write, and a power loss that leaves an empty state file
        is exactly what the ledger re-derivation covers.
        """
        if self._state_file:
            try:
                self._state_file.parent.mkdir(parents=True, exist_ok=True)
                data = {
                    "sequence": self._sequence,
                    "previous_hash": self._previous_hash,
                    "updated_at": utc_now().isoformat(),
                }
                temp_file = self._state_file.with_name(
                    f"{self._state_file.name}.{os.getpid()}.tmp"
                )
                temp_file.write_text(json.dumps(data, indent=2))
                os.replace(temp_file, self._state_file)
            except Exception as e:
                logger.warning(
                    "hash_chain.save_state_failed",
                    error=e,
                )

    def persist_state(self) -> None:
        """Persist chain state on shutdown.

        A no-op in file-lock mode: every write there already persisted under
        the cross-process lock, and writing this process's in-process counter
        over a state file a sibling has since advanced would roll the shared
        source backwards. Outside that mode the single-writer contract holds,
        so the in-process counter is the state worth keeping.
        """
        if self._use_file_lock and self._state_file:
            return
        with self._lock:
            self._save_state()

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

    def _reconcile_with_ledger(self, state_loaded: bool) -> dict[str, Any]:
        """Re-anchor the in-process counter to the ledger when it is behind.

        Caller must hold whatever exclusive section protects the source, so
        the comparison and the repair are atomic against every other writer.

        Args:
            state_loaded: Whether a state file currently backs the counter.

        Returns:
            The ``source_reset`` annotation to carry on the entry that
            continues the chain, or an empty dict when nothing was repaired.

        Raises:
            HashChainSequenceRefusedError: The ledger exists but its tail
                cannot be read. Raised before any mutation.
        """
        if self._ledger is None:
            return {}

        try:
            tail = self._ledger.read()
        except OSError as e:
            raise HashChainSequenceRefusedError(
                manager="local",
                log_dir=str(self._ledger.log_dir),
                filename_pattern=self._ledger.filename_pattern,
                error=str(e),
            ) from e

        if tail is None:
            return {}

        would_mint = self._sequence + 1

        if self._sequence < tail.sequence:
            reason = "source_behind_ledger" if state_loaded else "state_unreadable"
            self._sequence = tail.sequence
            self._previous_hash = tail.current_hash
        elif (
            self._sequence == tail.sequence and self._previous_hash != tail.current_hash
        ):
            reason = "state_hash_stale"
            self._previous_hash = tail.current_hash
        else:
            return {}

        return record_source_reset(
            manager="local",
            reason=reason,
            observed=would_mint,
            adopted=tail.sequence,
            ledger_path=str(tail.path),
        )

    def _compute_and_persist(
        self,
        entry: dict[str, Any],
        annotations: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Increment sequence, compute hash, persist, and return enriched entry.

        Caller must hold both the in-process ``_lock`` and (when enabled)
        the cross-process file lock acquired via ``_locked_state_update()``.

        Annotations are merged **under** the computed hash, never added after
        it: a stamp written on top of a hashed entry makes that entry verify
        as modified.
        """
        self._sequence += 1

        # Add integrity info (without current_hash for now)
        entry["integrity"] = {
            **sanitize_integrity_annotations(annotations),
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

    def add_integrity(
        self,
        entry: dict[str, Any],
        *,
        annotations: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Add integrity fields to a log entry.

        Args:
            entry: Log entry dictionary
            annotations: Extra keys to carry inside ``integrity``, hashed
                together with the entry. The chain's own field names are
                stripped, so an annotation can never shadow one.

        Returns:
            Entry with integrity fields added

        Raises:
            HashChainSequenceRefusedError: A wired ledger exists but its tail
                cannot be read, so no sequence can be minted safely.
        """
        with self._lock:
            if self._state_file and self._use_file_lock:
                # Atomic load + compare + increment + save under cross-process
                # lock.
                with self._locked_state_update():
                    state_loaded = self._load_state()  # re-read latest from disk
                    repair = self._reconcile_with_ledger(state_loaded)
                    return self._compute_and_persist(
                        entry, {**repair, **(annotations or {})}
                    )
            repair = self._reconcile_with_ledger(self._state_loaded)
            return self._compute_and_persist(entry, {**repair, **(annotations or {})})

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
        """Reset chain state (use with caution!).

        Clears the source only. With a ledger reader wired, the next write
        re-anchors to the ledger's own tail unless the ledger files are
        removed as well — a reset alone does not restart the chain at 1.
        """
        with self._lock:
            self._sequence = 0
            self._previous_hash = self.GENESIS_HASH
            self._state_loaded = False
            if self._state_file:
                safe_unlink(self._state_file)
            logger.warning("hash_chain.chain_state_reset")


from baldur.utils.singleton import make_singleton_factory  # noqa: E402

get_hash_chain_manager, configure_hash_chain_manager, reset_hash_chain_manager = (
    make_singleton_factory("hash_chain_manager", HashChainManager)
)

__all__ = [
    "HashChainManager",
    "configure_hash_chain_manager",
    "get_hash_chain_manager",
    "reset_hash_chain_manager",
]
