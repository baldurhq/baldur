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
import os
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from baldur.audit.integrity.ledger_tail import LedgerTailReader
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


# =============================================================================
# Concurrency — the cross-process half of the guarantee
# =============================================================================

# One writer process: append ``entries`` chain entries against a shared state
# file and report the sequences it was handed. Run out of process on purpose —
# the thread-based case above passes with the file lock removed, because
# ``HashChainManager`` also holds an in-process ``RLock``. Only separate
# processes can fail when the file lock is gone, which is the deployment the
# feature is sold for: Gunicorn workers, a Celery worker and cron sharing one
# audit volume.
_WRITER_SOURCE = textwrap.dedent(
    """
    import json
    import sys
    from pathlib import Path

    from baldur.audit.integrity.local_manager import HashChainManager

    state_file, entries, use_file_lock, out_path = sys.argv[1:5]
    manager = HashChainManager(
        state_file=Path(state_file),
        use_file_lock=use_file_lock == "1",
    )
    sequences = [
        manager.add_integrity({"event": "e"})["integrity"]["sequence"]
        for _ in range(int(entries))
    ]
    Path(out_path).write_text(json.dumps(sequences))
    """
)

# Enough writes per process that the unlocked arm's overlap is unmistakable,
# few enough that the whole case stays well inside the suite's timeout.
_ENTRIES_PER_PROCESS = 15


def _run_writers(tmp_path: Path, processes: int, *, use_file_lock: bool) -> list[int]:
    """Drive one shared state file from ``processes`` OS processes."""
    script = tmp_path / "writer.py"
    script.write_text(_WRITER_SOURCE, encoding="utf-8")
    state_file = tmp_path / ".hash_chain_state.json"

    outputs = [tmp_path / f"sequences_{i}.json" for i in range(processes)]
    running = [
        subprocess.Popen(
            [
                sys.executable,
                str(script),
                str(state_file),
                str(_ENTRIES_PER_PROCESS),
                "1" if use_file_lock else "0",
                str(out),
            ],
        )
        for out in outputs
    ]
    for proc in running:
        assert proc.wait(timeout=90) == 0, "writer process failed"

    sequences: list[int] = []
    for out in outputs:
        sequences.extend(json.loads(out.read_text()))
    return sequences


class TestHashChainFileLockMultiprocess:
    """Unique sequences across processes — and duplicates without the lock.

    The negative arm is what makes the positive one mean something: a chain
    whose sequences collide has two entries claiming the same position, so the
    verifier cannot tell a reordering from a deletion.
    """

    @pytest.mark.parametrize("processes", [2, 4])
    def test_sequences_are_unique_across_processes_under_the_lock(
        self, tmp_path, processes
    ):
        sequences = _run_writers(tmp_path, processes, use_file_lock=True)

        expected_total = processes * _ENTRIES_PER_PROCESS
        assert len(sequences) == expected_total
        assert sorted(sequences) == list(range(1, expected_total + 1))

    def test_the_same_writers_collide_without_the_lock(self, tmp_path):
        """The control arm. Each unlocked process keeps its own in-memory
        counter from an empty state file and flushes only every tenth write,
        so both hand out sequence 1 — the exact multi-writer corruption the
        lock exists to prevent."""
        sequences = _run_writers(tmp_path, 2, use_file_lock=False)

        assert len(sequences) == 2 * _ENTRIES_PER_PROCESS
        assert len(set(sequences)) < len(sequences), "expected duplicate sequences"


# =============================================================================
# The state file's own lifecycle — replaced atomically, read under the lock
# =============================================================================

# One process that takes the sibling lock, announces it, and holds it. The
# constructor's read is only serialized with the saves if it waits here.
_LOCK_HOLDER_SOURCE = textwrap.dedent(
    """
    import sys
    import time

    from baldur.audit.checkpoint.file_lock import lock_file, unlock_file

    lock_path, hold_seconds = sys.argv[1], float(sys.argv[2])
    with open(lock_path, "a+b") as fh:
        lock_file(fh, blocking=True)
        sys.stdout.write("held\\n")
        sys.stdout.flush()
        time.sleep(hold_seconds)
        unlock_file(fh)
    """
)

# Long enough that a constructor which ignores the lock is unmistakable, short
# enough to stay well inside both the suite timeout and the lock primitive's
# own 5 s acquisition deadline.
_LOCK_HOLD_SECONDS = 1.0


class TestAtomicStateSaveBehavior:
    """``_save_state`` replaces the file; it never truncates it in place.

    Truncate-then-write is what left invalid JSON behind after an unclean
    kill — the very precondition the ledger re-derivation now recovers from,
    self-inflicted by the writer.
    """

    def test_a_failed_atomic_save_leaves_the_previous_state_parseable(self, tmp_path):
        state_file = tmp_path / ".hash_chain_state.json"
        mgr = HashChainManager(state_file=state_file, use_file_lock=False)
        mgr.add_integrity({"event": "first"})
        mgr._save_state()
        before = json.loads(state_file.read_text())

        with patch.object(
            Path, "write_text", autospec=True, side_effect=OSError("ENOSPC")
        ):
            mgr._sequence = 999
            mgr._save_state()

        assert json.loads(state_file.read_text()) == before

    def test_the_atomic_temp_name_carries_the_writers_pid(self, tmp_path):
        """A fixed shared temp name would let two processes truncate each
        other's half-written temp, under the lock or not."""
        state_file = tmp_path / ".hash_chain_state.json"
        mgr = HashChainManager(state_file=state_file, use_file_lock=False)

        with patch("os.replace", autospec=True) as replace_spy:
            mgr._save_state()

        replace_spy.assert_called_once()
        source, destination = replace_spy.call_args[0]
        assert Path(source).name == f"{state_file.name}.{os.getpid()}.tmp"
        assert Path(destination) == state_file

    def test_a_successful_atomic_save_leaves_no_temp_file_behind(self, tmp_path):
        state_file = tmp_path / ".hash_chain_state.json"
        mgr = HashChainManager(state_file=state_file, use_file_lock=False)

        mgr._save_state()

        assert state_file.exists()
        assert list(tmp_path.glob("*.tmp")) == []


class TestAtomicStatePersistBehavior:
    """Shutdown must not roll the shared source backwards."""

    def test_persist_state_is_a_no_op_while_the_file_lock_governs_the_source(
        self, tmp_path
    ):
        """Every write already saved under the cross-process lock, so writing
        this process's in-process counter over a state file a sibling has
        since advanced is a rewind, not a checkpoint."""
        state_file = tmp_path / ".hash_chain_state.json"
        mgr = HashChainManager(state_file=state_file, use_file_lock=True)
        mgr.add_integrity({"event": "first"})
        sibling_advanced = {"sequence": 150, "previous_hash": "sibling-hash"}
        state_file.write_text(json.dumps(sibling_advanced))
        mgr._sequence = 100

        mgr.persist_state()

        assert json.loads(state_file.read_text()) == sibling_advanced

    def test_persist_state_saves_the_in_process_counter_when_single_writer(
        self, tmp_path
    ):
        """Outside lock mode the single-writer contract holds, so the
        in-process counter is the state worth keeping."""
        state_file = tmp_path / ".hash_chain_state.json"
        mgr = HashChainManager(state_file=state_file, use_file_lock=False)
        mgr._sequence = 100
        mgr._previous_hash = "hash-100"

        mgr.persist_state()

        assert json.loads(state_file.read_text())["sequence"] == 100


class TestAtomicStateConstructorLoadBehavior:
    """The constructor's read is serialized with the saves.

    ``os.replace`` fails on Windows while any process holds the target open,
    so a sibling recycling under ``max_requests`` must not read the state file
    outside the lock.
    """

    def _hold_the_lock(self, tmp_path: Path, state_file: Path) -> subprocess.Popen:
        script = tmp_path / "lock_holder.py"
        script.write_text(_LOCK_HOLDER_SOURCE, encoding="utf-8")
        holder = subprocess.Popen(
            [
                sys.executable,
                str(script),
                str(state_file.with_suffix(".lock")),
                str(_LOCK_HOLD_SECONDS),
            ],
            stdout=subprocess.PIPE,
        )
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == b"held", "holder never took the lock"
        return holder

    def test_construction_waits_for_a_sibling_holding_the_state_lock(self, tmp_path):
        state_file = tmp_path / ".hash_chain_state.json"
        state_file.write_text(json.dumps({"sequence": 7, "previous_hash": "hash-7"}))
        holder = self._hold_the_lock(tmp_path, state_file)

        try:
            started = time.monotonic()
            mgr = HashChainManager(state_file=state_file, use_file_lock=True)
            waited = time.monotonic() - started
        finally:
            holder.wait(timeout=30)

        assert waited >= _LOCK_HOLD_SECONDS / 2
        assert mgr._sequence == 7

    def test_construction_without_contention_does_not_wait(self, tmp_path):
        """The control arm: without it the case above passes on any slow
        import rather than on the lock."""
        state_file = tmp_path / ".hash_chain_state.json"
        state_file.write_text(json.dumps({"sequence": 7, "previous_hash": "hash-7"}))

        started = time.monotonic()
        HashChainManager(state_file=state_file, use_file_lock=True)
        waited = time.monotonic() - started

        assert waited < _LOCK_HOLD_SECONDS / 2


class TestAtomicStateResetBehavior:
    """``reset()`` clears the source, not the ledger."""

    @pytest.mark.parametrize("use_file_lock", [True, False], ids=["locked", "unlocked"])
    def test_a_reset_source_re_anchors_to_the_ledger_on_the_next_write(
        self, tmp_path, use_file_lock
    ):
        """A reset alone does not restart the chain at 1 — the ledger files
        are still there, and the entry that continues the chain says so."""
        ledger_path = tmp_path / "audit_2026-09-07.jsonl"
        seed = HashChainManager(state_file=None, use_file_lock=False)
        lines = []
        for index in range(5):
            entry = seed.add_integrity({"event": f"seed.{index}"})
            lines.append(json.dumps(entry))
        ledger_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        mgr = HashChainManager(
            state_file=tmp_path / ".hash_chain_state.json",
            use_file_lock=use_file_lock,
            ledger=LedgerTailReader(tmp_path),
        )
        mgr.add_integrity({"event": "before-reset"})
        mgr.reset()
        entry = mgr.add_integrity({"event": "after-reset"})

        assert mgr._state_loaded is False
        assert entry["integrity"]["source_reset"]["reason"] == "state_unreadable"
        assert entry["integrity"]["sequence"] == 6
