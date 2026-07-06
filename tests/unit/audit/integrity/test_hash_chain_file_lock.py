"""Unit tests for ``HashChainManager`` cross-process file lock (#416 D22).

The 416 commit added ``use_file_lock`` (default ``True``) to
``HashChainManager.__init__`` and a ``_locked_state_update()`` context
manager that wraps ``add_integrity()`` with the cross-platform
``audit/checkpoint/file_lock.py`` lock primitive.

Covers:
- Constructor stores ``use_file_lock`` flag.
- ``add_integrity()`` calls ``_locked_state_update()`` only when both
  ``state_file`` and ``use_file_lock`` are set.
- Lock-mode persists state on every write (instead of every 10).
- Non-lock mode keeps the legacy "every 10 writes" save cadence.
- Lock file is created at the sibling ``.lock`` path.
- ``add_integrity()`` re-loads state from disk under the lock so a
  second writer in the same process cannot fork the chain.
- Sequence numbers stay monotonic across many writes.
- ``reset()`` clears state and removes the saved file.
"""

from __future__ import annotations

import json
import threading
from unittest.mock import patch

from baldur.audit.integrity.local_manager import HashChainManager

# =============================================================================
# Contract — constructor flag and default behavior
# =============================================================================


class TestHashChainManagerFileLockContract:
    """Hardcoded checks for the D22 ``use_file_lock`` API surface."""

    def test_use_file_lock_default_is_true(self, tmp_path):
        """D22: file lock is opt-out, not opt-in."""
        mgr = HashChainManager(state_file=tmp_path / ".state.json")
        assert mgr._use_file_lock is True

    def test_use_file_lock_false_is_respected(self, tmp_path):
        """``use_file_lock=False`` disables the lock path."""
        mgr = HashChainManager(state_file=tmp_path / ".state.json", use_file_lock=False)
        assert mgr._use_file_lock is False

    def test_state_file_none_does_not_create_lock(self):
        """No state file → no lock file (single-process in-memory mode)."""
        mgr = HashChainManager(state_file=None, use_file_lock=True)
        # add_integrity must work without crashing — just no lock path.
        result = mgr.add_integrity({"event": "x"})
        assert result["integrity"]["sequence"] == 1


# =============================================================================
# Behavior — locked path is invoked, persistence cadence, monotonicity
# =============================================================================


class TestHashChainManagerFileLockBehavior:
    """Verifies the locked-state-update path is exercised when expected."""

    def test_locked_path_invoked_when_enabled(self, tmp_path):
        """``add_integrity()`` enters ``_locked_state_update()`` only when
        ``state_file`` AND ``use_file_lock`` are both set."""
        state_file = tmp_path / ".state.json"
        mgr = HashChainManager(state_file=state_file, use_file_lock=True)

        with patch.object(
            mgr, "_locked_state_update", wraps=mgr._locked_state_update
        ) as m_locked:
            mgr.add_integrity({"event": "x"})

        assert m_locked.call_count == 1

    def test_locked_path_skipped_when_disabled(self, tmp_path):
        """``use_file_lock=False`` does NOT enter ``_locked_state_update()``."""
        state_file = tmp_path / ".state.json"
        mgr = HashChainManager(state_file=state_file, use_file_lock=False)

        with patch.object(
            mgr, "_locked_state_update", wraps=mgr._locked_state_update
        ) as m_locked:
            mgr.add_integrity({"event": "x"})

        assert m_locked.call_count == 0

    def test_lock_mode_saves_state_every_write(self, tmp_path):
        """Multi-writer mode persists ``sequence`` after each ``add_integrity``."""
        state_file = tmp_path / ".state.json"
        mgr = HashChainManager(state_file=state_file, use_file_lock=True)

        for i in range(3):
            mgr.add_integrity({"event": f"e{i}"})
            # State file is current after every write.
            data = json.loads(state_file.read_text())
            assert data["sequence"] == i + 1

    def test_no_lock_mode_saves_state_every_10_writes(self, tmp_path):
        """Legacy non-lock cadence: state file flushed every 10 entries."""
        state_file = tmp_path / ".state.json"
        mgr = HashChainManager(state_file=state_file, use_file_lock=False)

        # First 9 writes do NOT create the file (default cadence).
        for i in range(9):
            mgr.add_integrity({"event": f"e{i}"})
        assert not state_file.exists()

        # 10th write triggers persistence.
        mgr.add_integrity({"event": "e9"})
        assert state_file.exists()
        data = json.loads(state_file.read_text())
        assert data["sequence"] == 10

    def test_sequences_are_monotonic_under_lock(self, tmp_path):
        """Sequences increase strictly under the locked path."""
        state_file = tmp_path / ".state.json"
        mgr = HashChainManager(state_file=state_file, use_file_lock=True)

        sequences = [
            mgr.add_integrity({"event": f"e{i}"})["integrity"]["sequence"]
            for i in range(50)
        ]

        assert sequences == list(range(1, 51))

    def test_sequences_unique_across_threads_under_lock(self, tmp_path):
        """Multiple threads in the same process must not produce duplicates."""
        state_file = tmp_path / ".state.json"
        mgr = HashChainManager(state_file=state_file, use_file_lock=True)
        results: list[int] = []
        results_lock = threading.Lock()

        def worker():
            for _ in range(20):
                seq = mgr.add_integrity({"event": "x"})["integrity"]["sequence"]
                with results_lock:
                    results.append(seq)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)

        assert len(results) == 80
        assert len(set(results)) == 80, "duplicate sequences detected"
        assert sorted(results) == list(range(1, 81))


# =============================================================================
# Side effects — lock file creation, state restore, reset
# =============================================================================


class TestHashChainManagerFileLockSideEffects:
    """External side effects — lock file path, state file lifecycle."""

    def test_lock_file_created_at_sibling_path(self, tmp_path):
        """Lock file path = state file with ``.lock`` suffix."""
        state_file = tmp_path / ".hash_chain_state.json"
        mgr = HashChainManager(state_file=state_file, use_file_lock=True)
        mgr.add_integrity({"event": "x"})

        lock_path = state_file.with_suffix(".lock")
        assert lock_path.exists()

    def test_load_state_called_under_lock_resumes_chain(self, tmp_path):
        """Two managers sharing the same state file — second one resumes
        from disk because the locked path re-reads ``_load_state()``."""
        state_file = tmp_path / ".state.json"

        a = HashChainManager(state_file=state_file, use_file_lock=True)
        a.add_integrity({"event": "first"})
        a.add_integrity({"event": "second"})
        first_seq = a.get_state()["sequence"]
        assert first_seq == 2

        # New instance must continue from sequence=2 → next is 3.
        b = HashChainManager(state_file=state_file, use_file_lock=True)
        next_entry = b.add_integrity({"event": "third"})
        assert next_entry["integrity"]["sequence"] == 3

    def test_previous_hash_chained_across_writes(self, tmp_path):
        """Each entry's ``previous_hash`` matches the prior ``current_hash``."""
        state_file = tmp_path / ".state.json"
        mgr = HashChainManager(state_file=state_file, use_file_lock=True)

        e1 = mgr.add_integrity({"event": "x"})
        e2 = mgr.add_integrity({"event": "y"})
        e3 = mgr.add_integrity({"event": "z"})

        assert e1["integrity"]["previous_hash"] == HashChainManager.GENESIS_HASH
        assert e2["integrity"]["previous_hash"] == e1["integrity"]["current_hash"]
        assert e3["integrity"]["previous_hash"] == e2["integrity"]["current_hash"]

    def test_reset_clears_state_file(self, tmp_path):
        """``reset()`` deletes the on-disk state file (use with caution)."""
        state_file = tmp_path / ".state.json"
        mgr = HashChainManager(state_file=state_file, use_file_lock=True)
        mgr.add_integrity({"event": "x"})
        assert state_file.exists()

        mgr.reset()

        assert not state_file.exists()
        assert mgr._sequence == 0
        assert mgr._previous_hash == HashChainManager.GENESIS_HASH
