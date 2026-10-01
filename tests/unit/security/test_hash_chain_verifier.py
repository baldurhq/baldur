"""
Tests for Hash Chain Verifier CLI Tool.

Tests:
- HashChainVerifier behaviour
- AuditIntegrityVerifier: whole-trail verification of ledger directories
- CLI output formats
- WAL verification
- Edge cases
"""

from __future__ import annotations

import json
import struct
import tempfile
import zlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from baldur.audit.integrity import (
    HashChainManager,
    HashChainVerifier,
    compute_hash,
    verify_audit_log_integrity,
)
from baldur.audit.verify_audit_integrity import (
    AuditIntegrityVerifier,
    VerificationResult,
    VerificationSummary,
    _verify_path,
    format_json_output,
    format_summary_output,
    format_text_output,
)


class TestComputeHash:
    """compute_hash."""

    def test_compute_hash_deterministic(self):
        """The same data gives the same hash."""
        data = {"key": "value", "number": 42}
        hash1 = compute_hash(data)
        hash2 = compute_hash(data)
        assert hash1 == hash2

    def test_compute_hash_different_data(self):
        """Different data gives different hashes."""
        data1 = {"key": "value1"}
        data2 = {"key": "value2"}
        assert compute_hash(data1) != compute_hash(data2)

    def test_compute_hash_key_order_independent(self):
        """Key order does not change the hash."""
        data1 = {"a": 1, "b": 2, "c": 3}
        data2 = {"c": 3, "a": 1, "b": 2}
        assert compute_hash(data1) == compute_hash(data2)

    def test_compute_hash_returns_hex_string(self):
        """A SHA-256 hex digest."""
        data = {"test": "data"}
        hash_value = compute_hash(data)
        assert len(hash_value) == 64  # SHA-256 hex
        assert all(c in "0123456789abcdef" for c in hash_value)


class TestHashChainManager:
    """HashChainManager."""

    def test_add_integrity_basic(self):
        """Integrity information is added."""
        manager = HashChainManager()
        entry = {"event": "test", "data": "value"}

        result = manager.add_integrity(entry)

        assert "integrity" in result
        assert result["integrity"]["sequence"] == 1
        assert result["integrity"]["previous_hash"] == "GENESIS"
        assert "current_hash" in result["integrity"]
        assert "timestamp" in result["integrity"]

    def test_add_integrity_chain(self):
        """Entries are chained."""
        manager = HashChainManager()

        entry1 = manager.add_integrity({"event": "first"})
        entry2 = manager.add_integrity({"event": "second"})

        assert entry1["integrity"]["sequence"] == 1
        assert entry2["integrity"]["sequence"] == 2
        assert (
            entry2["integrity"]["previous_hash"] == entry1["integrity"]["current_hash"]
        )

    def test_add_integrity_multiple_entries(self):
        """A chain of several entries."""
        manager = HashChainManager()
        entries = []

        for i in range(10):
            entry = manager.add_integrity({"event": f"event_{i}"})
            entries.append(entry)

        # Every sequence
        for i, entry in enumerate(entries):
            assert entry["integrity"]["sequence"] == i + 1

        # Every link
        for i in range(1, len(entries)):
            assert (
                entries[i]["integrity"]["previous_hash"]
                == entries[i - 1]["integrity"]["current_hash"]
            )

    def test_get_state(self):
        """State query."""
        manager = HashChainManager()
        manager.add_integrity({"event": "test"})

        state = manager.get_state()

        assert state["sequence"] == 1
        assert "previous_hash" in state

    def test_reset(self):
        """State reset."""
        manager = HashChainManager()
        manager.add_integrity({"event": "test"})
        manager.reset()

        state = manager.get_state()
        assert state["sequence"] == 0

    def test_persistence(self):
        """State persistence across manager instances."""
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "chain_state.json"

            # First manager: add entries
            manager1 = HashChainManager(state_file=state_file)
            for i in range(15):
                manager1.add_integrity({"event": f"event_{i}"})

            # Second manager: load state from file
            manager2 = HashChainManager(state_file=state_file)
            state = manager2.get_state()

            # Multi-writer mode (D22) is the default since 416: every write
            # is persisted under the cross-process file lock to guarantee
            # the state file matches the in-memory sequence on every commit.
            assert state["sequence"] == 15


class TestHashChainVerifierBehavior:
    """HashChainVerifier."""

    def _create_valid_chain(self, count: int = 5) -> list[dict[str, Any]]:
        """Build a valid hash chain."""
        manager = HashChainManager()
        return [
            manager.add_integrity({"event": f"event_{i}", "data": f"data_{i}"})
            for i in range(count)
        ]

    def test_verify_chain_valid(self):
        """A valid chain verifies."""
        entries = self._create_valid_chain(5)
        verifier = HashChainVerifier()

        is_valid, error = verifier.verify_chain(entries)

        assert is_valid is True
        assert error is None

    def test_verify_chain_empty(self):
        """An empty chain verifies."""
        verifier = HashChainVerifier()
        is_valid, error = verifier.verify_chain([])

        assert is_valid is True
        assert error is None

    def test_verify_chain_modified_entry(self):
        """A modified entry is detected."""
        entries = self._create_valid_chain(5)
        # Tamper with a middle entry
        entries[2]["data"] = "TAMPERED"

        verifier = HashChainVerifier()
        is_valid, error = verifier.verify_chain(entries)

        assert is_valid is False
        assert "hash mismatch" in error.lower()

    def test_verify_chain_missing_entry(self):
        """A removed entry is detected."""
        entries = self._create_valid_chain(5)
        # Remove a middle entry
        del entries[2]

        verifier = HashChainVerifier()
        is_valid, error = verifier.verify_chain(entries)

        assert is_valid is False
        assert "missing" in error.lower()

    def test_verify_chain_broken_link(self):
        """A broken link is detected."""
        entries = self._create_valid_chain(5)
        # Rewrite a previous_hash
        entries[3]["integrity"]["previous_hash"] = "FAKE_HASH"

        verifier = HashChainVerifier()
        is_valid, error = verifier.verify_chain(entries)

        assert is_valid is False
        assert "broken" in error.lower() or "mismatch" in error.lower()

    def test_find_tampering_all_issues(self):
        """Every issue is found."""
        entries = self._create_valid_chain(10)

        # Several kinds of damage
        entries[2]["data"] = "TAMPERED"  # modified
        entries[5]["integrity"]["previous_hash"] = "FAKE"  # link rewritten
        del entries[7]  # removed (sequence 8)

        verifier = HashChainVerifier()
        issues = verifier.find_tampering(entries)

        issue_types = {(i["type"], i["sequence"]) for i in issues}
        assert ("entry_modified", 3) in issue_types
        assert ("chain_broken", 6) in issue_types
        assert ("missing_entry", 8) in issue_types


def _write_ledger(path: Path, entries: list[dict[str, Any]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")
    return path


class TestAuditIntegrityVerifierBehavior:
    """AuditIntegrityVerifier: every ledger file of one chain is one trail."""

    def _chain(self, count: int) -> list[dict[str, Any]]:
        manager = HashChainManager()
        return [manager.add_integrity({"event": f"event_{i}"}) for i in range(count)]

    def test_verify_paths_valid(self):
        """One untouched ledger file verifies."""
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_ledger(Path(tmpdir) / "audit_2026-09-28.jsonl", self._chain(5))

            summary = AuditIntegrityVerifier().verify_paths([Path(tmpdir)])

            assert summary.is_valid is True
            assert summary.total_trails == 1
            assert summary.total_entries == 5
            assert summary.total_issues == 0

    def test_verify_paths_reports_a_modified_entry_with_its_file(self):
        """A tampered entry is reported with its sequence, file and line."""
        with tempfile.TemporaryDirectory() as tmpdir:
            entries = self._chain(5)
            entries[2]["event"] = "TAMPERED"
            ledger = _write_ledger(Path(tmpdir) / "audit_2026-09-28.jsonl", entries)

            summary = AuditIntegrityVerifier().verify_paths([Path(tmpdir)])

            assert summary.is_valid is False
            issue = summary.results[0].issues[0]
            assert (issue["type"], issue["sequence"]) == ("entry_modified", 3)
            assert (issue["file"], issue["line"]) == (str(ledger), 3)

    def test_days_of_one_chain_verify_as_one_trail(self):
        """A chain continued across daily files verifies intact."""
        with tempfile.TemporaryDirectory() as tmpdir:
            entries = self._chain(6)
            _write_ledger(Path(tmpdir) / "audit_2026-09-28.jsonl", entries[:2])
            _write_ledger(Path(tmpdir) / "audit_2026-09-29.jsonl", entries[2:4])
            _write_ledger(Path(tmpdir) / "audit_2026-09-30.jsonl", entries[4:])

            summary = AuditIntegrityVerifier().verify_paths([Path(tmpdir)])

            assert summary.is_valid is True
            assert summary.total_trails == 1
            assert summary.results[0].first_sequence == 1
            assert summary.results[0].last_sequence == 6
            assert len(summary.results[0].files) == 3

    def test_recursive_merges_sub_directories(self):
        """With --recursive, a sub-directory's files join the same trail."""
        with tempfile.TemporaryDirectory() as tmpdir:
            entries = self._chain(5)
            _write_ledger(Path(tmpdir) / "audit_2026-09-28.jsonl", entries[:2])
            _write_ledger(
                Path(tmpdir) / "subdir" / "audit_2026-09-29.jsonl", entries[2:]
            )
            verifier = AuditIntegrityVerifier()

            flat = verifier.verify_paths([Path(tmpdir)], recursive=False)
            recursive = verifier.verify_paths([Path(tmpdir)], recursive=True)

            assert flat.total_entries == 2
            assert recursive.total_entries == 5
            assert recursive.is_valid is True

    def test_pattern_selects_custom_named_files(self):
        """--pattern replaces the ledger file-name shape."""
        with tempfile.TemporaryDirectory() as tmpdir:
            entries = self._chain(4)
            _write_ledger(Path(tmpdir) / "ledger_1.ndjson", entries[:2])
            _write_ledger(Path(tmpdir) / "ledger_2.ndjson", entries[2:])
            (Path(tmpdir) / "other.txt").write_text("not audit")
            verifier = AuditIntegrityVerifier()

            by_shape = verifier.verify_paths([Path(tmpdir)])
            by_pattern = verifier.verify_paths([Path(tmpdir)], pattern="*.ndjson")

            assert by_shape.error is not None
            assert by_pattern.is_valid is True
            assert by_pattern.total_entries == 4

    def test_chain_state_files_are_never_read(self):
        """The state, lock and temp files beside a ledger are not selected."""
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_ledger(Path(tmpdir) / "audit_2026-09-28.jsonl", self._chain(3))
            (Path(tmpdir) / ".hash_chain_state.json").write_text(
                json.dumps({"sequence": 3, "previous_hash": "x"}, indent=2)
            )
            (Path(tmpdir) / ".hash_chain_state.lock").write_text("")

            summary = AuditIntegrityVerifier().verify_paths([Path(tmpdir)], pattern="*")

            assert summary.is_valid is True

    def test_no_ledger_file_is_not_intact(self):
        """A directory holding no ledger file verifies nothing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            summary = AuditIntegrityVerifier().verify_paths([Path(tmpdir)])

            assert summary.is_valid is False
            assert "no audit ledger files found" in summary.error

    def test_a_missing_path_is_not_verified(self):
        """A path that does not exist stops the run."""
        assert (
            _verify_path(
                AuditIntegrityVerifier(),
                [Path("/nonexistent/audit")],
                wal_mode=False,
                recursive=False,
                pattern=None,
            )
            is None
        )


class TestWALVerification:
    """WAL verification."""

    def _create_valid_wal_file(
        self, tmpdir: str, filename: str = "test.wal", count: int = 5
    ) -> Path:
        """Create a valid WAL file."""
        file_path = Path(tmpdir) / filename

        with open(file_path, "wb") as f:
            for i in range(count):
                entry = {
                    "seq": i + 1,
                    "ts": datetime.now(UTC).timestamp(),
                    "data": {"event": f"event_{i}"},
                }
                entry_bytes = json.dumps(entry, separators=(",", ":")).encode("utf-8")
                checksum = zlib.crc32(entry_bytes) & 0xFFFFFFFF
                checksum_str = f"{checksum:08x}"

                # Format: [4-byte length][checksum:8][entry_bytes]
                record = (
                    struct.pack(">I", len(entry_bytes))
                    + checksum_str.encode("ascii")
                    + entry_bytes
                )
                f.write(record)

        return file_path

    def test_verify_wal_valid(self):
        """A valid WAL verifies."""
        with tempfile.TemporaryDirectory() as tmpdir:
            wal_file = self._create_valid_wal_file(tmpdir, count=5)
            verifier = AuditIntegrityVerifier()

            result = verifier._verify_wal_file(wal_file)

            assert result.is_valid is True
            assert result.total_entries == 5

    def test_verify_wal_corrupted_checksum(self):
        """A WAL with a damaged checksum fails."""
        with tempfile.TemporaryDirectory() as tmpdir:
            wal_file = self._create_valid_wal_file(tmpdir, count=3)

            # Damage the checksum
            with open(wal_file, "r+b") as f:
                f.seek(4)  # checksum position
                f.write(b"BADCHECK")  # wrong checksum

            verifier = AuditIntegrityVerifier()
            result = verifier._verify_wal_file(wal_file)

            assert result.is_valid is False
            assert any(i["type"] == "checksum_mismatch" for i in result.issues)

    def test_verify_wal_directory(self):
        """A WAL directory verifies."""
        with tempfile.TemporaryDirectory() as tmpdir:
            self._create_valid_wal_file(tmpdir, "wal_001.wal", 3)
            self._create_valid_wal_file(tmpdir, "wal_002.wal", 5)

            verifier = AuditIntegrityVerifier()
            result = verifier.verify_wal_directory(Path(tmpdir))

            assert result.is_valid is True
            assert result.total_entries == 8


class TestOutputFormatsBehavior:
    """Output formats."""

    def _create_test_summary(self) -> VerificationSummary:
        """A summary of one intact trail and one with issues."""
        return VerificationSummary(
            paths=["/var/log/audit"],
            results=[
                VerificationResult(
                    name="default",
                    is_valid=True,
                    total_entries=80,
                    first_sequence=1,
                    last_sequence=80,
                    files=["/var/log/audit/audit_2026-09-28.jsonl"],
                ),
                VerificationResult(
                    name="worker",
                    is_valid=False,
                    total_entries=20,
                    first_sequence=1,
                    last_sequence=20,
                    files=["/var/log/audit/audit_2026-09-28_worker.jsonl"],
                    issues=[
                        {
                            "type": "entry_modified",
                            "sequence": 5,
                            "file": "/var/log/audit/audit_2026-09-28_worker.jsonl",
                            "line": 5,
                            "message": "Entry 5 has been modified: hash mismatch",
                        },
                        {
                            "type": "chain_broken",
                            "sequence": 10,
                            "file": "/var/log/audit/audit_2026-09-28_worker.jsonl",
                            "line": 10,
                            "message": "Chain broken at entry 10",
                        },
                    ],
                ),
            ],
        )

    def test_format_text_output(self):
        """Text output: one block per trail, ASCII status markers."""
        summary = self._create_test_summary()
        output = format_text_output(summary)

        assert "Audit Log Integrity Verification Report" in output
        assert "Trail: default  [OK]" in output
        assert "Trail: worker  [FAIL]" in output
        assert "Entries: 80 (sequences 1-80)" in output
        assert output.isascii()

    def test_format_text_output_verbose(self):
        """Verbose text output lists every file and issue."""
        summary = self._create_test_summary()
        output = format_text_output(summary, verbose=True)

        assert "audit_2026-09-28.jsonl" in output
        assert "audit_2026-09-28_worker.jsonl:5" in output
        assert "hash mismatch" in output

    def test_format_json_output(self):
        """JSON output carries each trail's span, issues and notes."""
        summary = self._create_test_summary()
        output = format_json_output(summary)

        data = json.loads(output)
        assert data["summary"]["trails"] == 2
        assert data["summary"]["valid_trails"] == 1
        assert data["summary"]["is_valid"] is False
        worker = data["trails"][1]
        assert worker["partition"] == "worker"
        assert (worker["first_sequence"], worker["last_sequence"]) == (1, 20)
        assert worker["notes"] == []

    def test_format_summary_output(self):
        """Summary output: one line per trail, then the total."""
        summary = self._create_test_summary()
        output = format_summary_output(summary)

        lines = output.splitlines()
        assert lines[0] == "PASS default: 80 entries, 0 issues"
        assert lines[1] == "FAIL worker: 20 entries, 2 issues"
        assert lines[2].startswith("FAIL: 1/2 trails valid, 100 entries, 2 issues")

    def test_format_summary_output_pass(self):
        """An intact summary passes."""
        summary = VerificationSummary(
            results=[
                VerificationResult(name="default", is_valid=True, total_entries=50)
            ]
        )
        output = format_summary_output(summary)

        assert output.splitlines()[-1].startswith("PASS")


class TestVerifyAuditLogIntegrityBehavior:
    """verify_audit_log_integrity."""

    def test_verify_valid_file(self):
        """A valid file verifies."""
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = Path(tmpdir) / "audit.jsonl"
            manager = HashChainManager()

            with open(file_path, "w") as f:
                for i in range(5):
                    entry = manager.add_integrity({"event": f"event_{i}"})
                    f.write(json.dumps(entry) + "\n")

            is_valid, issues = verify_audit_log_integrity(file_path)

            assert is_valid is True
            assert issues == []

    def test_verify_nonexistent_file(self):
        """A file that does not exist has nothing to verify."""
        is_valid, issues = verify_audit_log_integrity(Path("/nonexistent/file.jsonl"))

        assert is_valid is True
        assert issues == []

    def test_verify_invalid_json(self):
        """A line that does not parse is an unreadable row."""
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = Path(tmpdir) / "audit.jsonl"
            file_path.write_text("not valid json\n")

            is_valid, issues = verify_audit_log_integrity(file_path)

            assert is_valid is False
            assert len(issues) > 0
            assert issues[0]["type"] == "unreadable_row"
            assert issues[0]["line"] == 1


class TestEdgeCases:
    """Edge cases."""

    def test_single_entry_chain(self):
        """A single-entry chain."""
        manager = HashChainManager()
        entries = [manager.add_integrity({"event": "single"})]

        verifier = HashChainVerifier()
        is_valid, error = verifier.verify_chain(entries)

        assert is_valid is True

    def test_large_chain(self):
        """A large chain."""
        manager = HashChainManager()
        entries = [
            manager.add_integrity({"event": f"event_{i}", "data": "x" * 100})
            for i in range(1000)
        ]

        verifier = HashChainVerifier()
        is_valid, error = verifier.verify_chain(entries)

        assert is_valid is True

    def test_unicode_data(self):
        """Non-ASCII data."""
        manager = HashChainManager()
        entries = [
            manager.add_integrity({"event": "한글 test", "emoji": "\U0001f512"}),
            manager.add_integrity({"event": "日本語", "data": "中文"}),
        ]

        verifier = HashChainVerifier()
        is_valid, error = verifier.verify_chain(entries)

        assert is_valid is True

    def test_nested_data(self):
        """Nested data structures."""
        manager = HashChainManager()
        entries = [
            manager.add_integrity(
                {
                    "event": "nested",
                    "data": {
                        "level1": {"level2": {"level3": [1, 2, 3, {"key": "value"}]}}
                    },
                }
            )
        ]

        verifier = HashChainVerifier()
        is_valid, error = verifier.verify_chain(entries)

        assert is_valid is True
