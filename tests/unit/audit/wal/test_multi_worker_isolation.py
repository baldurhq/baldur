"""
Unit tests for WAL multi-worker isolation (#470 D7).

Phase 2 of #470: ResilientStorageBackend's lazy recovery loop now
fires once-per-Redis-blip per worker, which promotes a pre-existing
WAL multi-worker isolation defect from latent (cold-deploy only) to
routine. This module covers the three fixes:

- G3: ``cleanup_processed(mode="runtime")`` must not delete a peer
  worker's still-active WAL file.
- G4: ``recover_unprocessed(mode="runtime")`` must not over-replay
  peer workers' entries.
- G5: ``WriteAheadLog._init_or_recover()`` must not inherit a peer
  worker's sequence number on fresh-process boot.

The default ``mode="startup"`` preserves the existing 7-caller cross-PID
glob semantics — that path is also exercised so the regression is locked
down.

Verification techniques per UNIT_TEST_GUIDELINES §8:
- Parametrize: ``mode ∈ {runtime, startup}`` × ``pid_match ∈ {self, peer}``
- Side effects: file deletion (``cleanup_processed``)
- Data immutability: peer-PID files survive runtime-mode operations
- State transition: ``_init_or_recover`` sequence assignment
"""

from __future__ import annotations

import json
import os
import struct
import tempfile
import zlib
from pathlib import Path

import pytest

from baldur.audit.wal import WriteAheadLog
from baldur.audit.wal._models import WALConfig
from baldur.audit.wal._reader import _wal_glob_pattern

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def temp_wal_dir():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir


@pytest.fixture
def wal_config(temp_wal_dir):
    return WALConfig(
        wal_dir=temp_wal_dir,
        max_file_size_mb=1,
        sync_on_write=False,
        max_files=10,
        file_prefix="test_wal",
    )


def _create_raw_wal_file(filepath: Path, entries: list[dict]) -> None:
    """Helper: write a raw WAL file with the on-disk format that
    ``_read_wal_file`` understands (header + length-prefixed JSON
    records with CRC32 hex checksum). Bypasses ``WriteAheadLog`` so the
    test can stamp arbitrary PIDs into the filename.
    """
    with open(filepath, "wb") as f:
        f.write(b"AWAL")
        f.write(struct.pack(">I", 1))
        for entry_dict in entries:
            data = json.dumps(entry_dict).encode("utf-8")
            checksum = format(zlib.crc32(data) & 0xFFFFFFFF, "08x")
            f.write(struct.pack(">I", len(data)))
            f.write(checksum.encode("ascii"))
            f.write(data)


def _self_pid_filename(prefix: str, suffix: str = "001") -> str:
    return f"{prefix}_{suffix}_{os.getpid()}.wal"


def _peer_pid_filename(prefix: str, suffix: str = "001") -> str:
    """A PID we are guaranteed not to have. Using a value far from
    self-PID also avoids the lexicographic-glob accident where a peer
    file sorts adjacent to a self-PID file.
    """
    peer = os.getpid() + 99999
    return f"{prefix}_{suffix}_{peer}.wal"


# =============================================================================
# Contract: _wal_glob_pattern helper
# =============================================================================


class TestWALGlobPatternContract:
    """``_wal_glob_pattern`` is the single point of truth for the
    runtime/startup branch — verifying the literal pattern strings
    pins the contract for consumers (``recover_unprocessed``,
    ``cleanup_processed``, ``_init_or_recover``).
    """

    def test_startup_mode_matches_all_pids(self):
        pattern = _wal_glob_pattern("audit_wal", "startup")
        assert pattern == "audit_wal_*.wal"

    def test_runtime_mode_filters_to_self_pid(self):
        pattern = _wal_glob_pattern("audit_wal", "runtime")
        assert pattern == f"audit_wal_*_{os.getpid()}.wal"

    def test_runtime_pattern_uses_current_pid_at_call_time(self):
        """Pattern is evaluated against the live ``os.getpid()`` so a
        forked child receives its own filter.
        """
        # Same PID twice → same string (deterministic).
        p1 = _wal_glob_pattern("p", "runtime")
        p2 = _wal_glob_pattern("p", "runtime")
        assert p1 == p2
        assert str(os.getpid()) in p1


# =============================================================================
# Behavior: recover_unprocessed mode parameter (G4)
# =============================================================================


class TestWALRecoverUnprocessedMode:
    """``recover_unprocessed(mode=...)`` glob-filter behavior."""

    def test_default_mode_is_startup(self, temp_wal_dir, wal_config):
        """Regression guard: existing 7 callers rely on cross-PID glob.
        Default must not silently change.
        """
        wal_dir = Path(temp_wal_dir)
        _create_raw_wal_file(
            wal_dir / _self_pid_filename("test_wal", "001"),
            [{"seq": 1, "ts": 1.0, "data": {"e": "self"}}],
        )
        _create_raw_wal_file(
            wal_dir / _peer_pid_filename("test_wal", "002"),
            [{"seq": 2, "ts": 2.0, "data": {"e": "peer"}}],
        )

        wal = WriteAheadLog(config=wal_config)
        try:
            # No mode kwarg → should observe both files.
            entries = wal.recover_unprocessed(last_processed_seq=0)
            seqs = sorted(e.sequence for e in entries)
            assert seqs == [1, 2]
        finally:
            wal.close()

    def test_runtime_mode_returns_only_self_pid_entries(self, temp_wal_dir, wal_config):
        """G4: runtime mode must skip peer-PID files so the recovery
        thread does not over-replay peer workers' entries to Redis.
        """
        wal_dir = Path(temp_wal_dir)
        _create_raw_wal_file(
            wal_dir / _self_pid_filename("test_wal", "001"),
            [
                {"seq": 1, "ts": 1.0, "data": {"e": "self_a"}},
                {"seq": 3, "ts": 3.0, "data": {"e": "self_b"}},
            ],
        )
        _create_raw_wal_file(
            wal_dir / _peer_pid_filename("test_wal", "002"),
            [{"seq": 2, "ts": 2.0, "data": {"e": "peer"}}],
        )

        wal = WriteAheadLog(config=wal_config)
        try:
            entries = wal.recover_unprocessed(last_processed_seq=0, mode="runtime")
            seqs = sorted(e.sequence for e in entries)
            assert seqs == [1, 3]
            # Confirm the peer entry was filtered out, not just absent.
            payloads = [e.data.get("e") for e in entries]
            assert "peer" not in payloads
        finally:
            wal.close()

    def test_startup_mode_absorbs_peer_orphans(self, temp_wal_dir, wal_config):
        """Startup-mode default contract preserves orphan absorption —
        crashed peer workers' WAL files are recovered on next process
        boot.
        """
        wal_dir = Path(temp_wal_dir)
        _create_raw_wal_file(
            wal_dir / _peer_pid_filename("test_wal", "001"),
            [{"seq": 5, "ts": 5.0, "data": {"e": "orphan"}}],
        )

        wal = WriteAheadLog(config=wal_config)
        try:
            entries = wal.recover_unprocessed(last_processed_seq=0, mode="startup")
            assert len(entries) == 1
            assert entries[0].sequence == 5
        finally:
            wal.close()

    def test_runtime_mode_with_no_self_pid_files_returns_empty(
        self, temp_wal_dir, wal_config
    ):
        """Edge case: only peer files exist → runtime mode sees
        nothing to replay.
        """
        wal_dir = Path(temp_wal_dir)
        _create_raw_wal_file(
            wal_dir / _peer_pid_filename("test_wal", "001"),
            [{"seq": 1, "ts": 1.0, "data": {"e": "peer"}}],
        )

        wal = WriteAheadLog(config=wal_config)
        try:
            entries = wal.recover_unprocessed(last_processed_seq=0, mode="runtime")
            assert entries == []
        finally:
            wal.close()


# =============================================================================
# Behavior: cleanup_processed mode parameter (G3 — data loss prevention)
# =============================================================================


class TestWALCleanupProcessedMode:
    """``cleanup_processed(mode=...)`` glob-filter behavior. The
    runtime branch is the load-bearing safety fix: a peer's still-active
    WAL must never be deleted by another worker's recovery thread.
    """

    def test_default_mode_is_startup(self, temp_wal_dir, wal_config):
        """Regression: existing callers (sync_worker, reconciler, etc.)
        rely on default startup behavior to drain orphans.
        """
        wal_dir = Path(temp_wal_dir)
        peer_file = wal_dir / _peer_pid_filename("test_wal", "001")
        _create_raw_wal_file(peer_file, [{"seq": 1, "ts": 1.0, "data": {}}])

        wal = WriteAheadLog(config=wal_config)
        try:
            deleted = wal.cleanup_processed(last_processed_seq=10)
        finally:
            wal.close()

        # Default startup mode: peer file with max_seq=1 ≤ 10 is deleted.
        assert deleted == 1
        assert not peer_file.exists()

    def test_runtime_mode_does_not_delete_peer_pid_files(
        self, temp_wal_dir, wal_config
    ):
        """G3 (data loss): runtime mode must skip peer-PID files even
        when their max_seq ≤ last_processed_seq. The peer worker is
        still actively writing to that file — deleting it loses data.
        """
        wal_dir = Path(temp_wal_dir)
        peer_file = wal_dir / _peer_pid_filename("test_wal", "001")
        self_file = wal_dir / _self_pid_filename("test_wal", "002")
        _create_raw_wal_file(peer_file, [{"seq": 1, "ts": 1.0, "data": {"e": "peer"}}])
        _create_raw_wal_file(self_file, [{"seq": 2, "ts": 2.0, "data": {"e": "self"}}])

        wal = WriteAheadLog(config=wal_config)
        try:
            deleted = wal.cleanup_processed(last_processed_seq=100, mode="runtime")
        finally:
            wal.close()

        # Only self-PID file deleted; peer survives.
        assert deleted == 1
        assert peer_file.exists()
        assert not self_file.exists()

    def test_startup_mode_deletes_peer_pid_files_below_threshold(
        self, temp_wal_dir, wal_config
    ):
        """Startup-mode contract: cross-PID glob still drains orphans."""
        wal_dir = Path(temp_wal_dir)
        peer_file = wal_dir / _peer_pid_filename("test_wal", "001")
        _create_raw_wal_file(peer_file, [{"seq": 5, "ts": 5.0, "data": {}}])

        wal = WriteAheadLog(config=wal_config)
        try:
            deleted = wal.cleanup_processed(last_processed_seq=100, mode="startup")
        finally:
            wal.close()

        assert deleted == 1
        assert not peer_file.exists()

    def test_neither_mode_deletes_files_above_threshold(self, temp_wal_dir, wal_config):
        """A WAL file with unprocessed entries (max_seq >
        last_processed_seq) must survive both modes.
        """
        wal_dir = Path(temp_wal_dir)
        self_file = wal_dir / _self_pid_filename("test_wal", "001")
        _create_raw_wal_file(self_file, [{"seq": 100, "ts": 100.0, "data": {}}])

        wal = WriteAheadLog(config=wal_config)
        try:
            assert wal.cleanup_processed(last_processed_seq=50, mode="runtime") == 0
            assert wal.cleanup_processed(last_processed_seq=50, mode="startup") == 0
        finally:
            wal.close()

        assert self_file.exists()


# =============================================================================
# Behavior: _init_or_recover self-PID filter (G5 — sequence collision)
# =============================================================================


class TestWALInitSelfPidFilter:
    """``_init_or_recover`` must read this worker's last sequence from
    its own files only. Inheriting a peer's sequence number causes the
    new worker's first writes to collide with the peer's still-growing
    sequence (#470 G5).
    """

    def test_fresh_worker_does_not_inherit_peer_sequence(
        self, temp_wal_dir, wal_config
    ):
        """A peer-PID file with high sequence exists in the shared
        ``wal_dir``. New worker boot must start at sequence 0.
        """
        wal_dir = Path(temp_wal_dir)
        _create_raw_wal_file(
            wal_dir / _peer_pid_filename("test_wal", "001"),
            [
                {"seq": 1000, "ts": 1.0, "data": {"e": "peer_high"}},
                {"seq": 1001, "ts": 2.0, "data": {"e": "peer_higher"}},
            ],
        )

        wal = WriteAheadLog(config=wal_config)
        try:
            assert wal._sequence == 0
        finally:
            wal.close()

    def test_existing_pid_recovers_own_last_sequence(self, temp_wal_dir, wal_config):
        """Re-opening a WAL on an existing PID (e.g., re-init cycle in
        tests) must recover the highest self-PID sequence — the
        non-collision case the filter must still support.
        """
        wal_dir = Path(temp_wal_dir)
        _create_raw_wal_file(
            wal_dir / _self_pid_filename("test_wal", "001"),
            [
                {"seq": 5, "ts": 1.0, "data": {}},
                {"seq": 7, "ts": 2.0, "data": {}},
            ],
        )

        wal = WriteAheadLog(config=wal_config)
        try:
            assert wal._sequence == 7
        finally:
            wal.close()

    def test_fresh_worker_with_only_peer_files_is_at_sequence_zero(
        self, temp_wal_dir, wal_config
    ):
        """Mixed peers, no self-PID file → start at zero. Confirms the
        glob filter, not just an absent-file accident, drives the
        outcome.
        """
        wal_dir = Path(temp_wal_dir)
        for suffix in ("001", "002", "003"):
            _create_raw_wal_file(
                wal_dir / _peer_pid_filename("test_wal", suffix),
                [{"seq": 999, "ts": 1.0, "data": {}}],
            )

        wal = WriteAheadLog(config=wal_config)
        try:
            assert wal._sequence == 0
            # First write produces sequence 1, NOT 1000 — no collision.
            seq = wal.write({"event": "first"})
            assert seq == 1
        finally:
            wal.close()


# =============================================================================
# Integration-shape: full self-vs-peer parametrize matrix
# =============================================================================


class TestWALModeMatrixBehavior:
    """Parametrized mode × pid_match matrix (per design-doc Testability
    Notes). Locks in the 4-cell decision table for both reader methods.
    """

    @pytest.mark.parametrize(
        ("mode", "pid_kind", "expected_visible"),
        [
            ("runtime", "self", True),
            ("runtime", "peer", False),
            ("startup", "self", True),
            ("startup", "peer", True),
        ],
    )
    def test_recover_unprocessed_visibility_matrix(
        self, temp_wal_dir, wal_config, mode, pid_kind, expected_visible
    ):
        wal_dir = Path(temp_wal_dir)
        if pid_kind == "self":
            filename = _self_pid_filename("test_wal", "001")
        else:
            filename = _peer_pid_filename("test_wal", "001")
        _create_raw_wal_file(wal_dir / filename, [{"seq": 1, "ts": 1.0, "data": {}}])

        wal = WriteAheadLog(config=wal_config)
        try:
            entries = wal.recover_unprocessed(last_processed_seq=0, mode=mode)
        finally:
            wal.close()

        assert (len(entries) == 1) is expected_visible

    @pytest.mark.parametrize(
        ("mode", "pid_kind", "expected_deleted"),
        [
            ("runtime", "self", True),
            ("runtime", "peer", False),
            ("startup", "self", True),
            ("startup", "peer", True),
        ],
    )
    def test_cleanup_processed_visibility_matrix(
        self, temp_wal_dir, wal_config, mode, pid_kind, expected_deleted
    ):
        wal_dir = Path(temp_wal_dir)
        if pid_kind == "self":
            filename = _self_pid_filename("test_wal", "001")
        else:
            filename = _peer_pid_filename("test_wal", "001")
        target = wal_dir / filename
        _create_raw_wal_file(target, [{"seq": 1, "ts": 1.0, "data": {}}])

        wal = WriteAheadLog(config=wal_config)
        try:
            wal.cleanup_processed(last_processed_seq=100, mode=mode)
        finally:
            wal.close()

        # Self-PID files are protected when they ARE the current
        # writer's active file — but in this test the WAL was just
        # closed, so ``self._current_file`` no longer protects it.
        # Either way the matrix predicts file existence.
        assert (not target.exists()) is expected_deleted
