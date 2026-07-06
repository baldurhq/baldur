"""
Tests for hash chain integrity verification.
"""

import json
import tempfile
from pathlib import Path

from baldur.audit.integrity import (
    HashChainManager,
    HashChainVerifier,
    verify_audit_log_integrity,
)


class TestHashChainManager:
    """Tests for HashChainManager."""

    def test_add_integrity_first_entry(self):
        """Test first entry has sequence 1 and GENESIS previous hash."""
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "state.json"
            manager = HashChainManager(state_file)

            entry = {"event": "test", "timestamp": "2024-01-01T00:00:00Z"}
            result = manager.add_integrity(entry)

            assert "integrity" in result
            assert result["integrity"]["sequence"] == 1
            assert result["integrity"]["previous_hash"] == "GENESIS"
            assert "current_hash" in result["integrity"]

    def test_add_integrity_chain_continuation(self):
        """Test that entries form a proper chain."""
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "state.json"
            manager = HashChainManager(state_file)

            entry1 = {"event": "first"}
            result1 = manager.add_integrity(entry1)

            entry2 = {"event": "second"}
            result2 = manager.add_integrity(entry2)

            assert result2["integrity"]["sequence"] == 2
            assert (
                result2["integrity"]["previous_hash"]
                == result1["integrity"]["current_hash"]
            )

    def test_state_persistence(self):
        """Test that state persists across instances."""
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "state.json"

            # Create first manager and add entries
            manager1 = HashChainManager(state_file)
            manager1.add_integrity({"event": "first"})
            result2 = manager1.add_integrity({"event": "second"})
            last_hash = result2["integrity"]["current_hash"]
            manager1._save_state()

            # Create new manager instance
            manager2 = HashChainManager(state_file)
            result = manager2.add_integrity({"event": "third"})

            assert result["integrity"]["sequence"] == 3
            assert result["integrity"]["previous_hash"] == last_hash

    def test_get_state(self):
        """Test getting current state."""
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "state.json"
            manager = HashChainManager(state_file)
            manager.add_integrity({"event": "test"})

            state = manager.get_state()

            assert "sequence" in state
            assert "previous_hash" in state
            assert state["sequence"] == 1


class TestHashChainVerifier:
    """Tests for HashChainVerifier."""

    def test_verify_chain_correct(self):
        """Test verification of correctly chained entries."""
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "state.json"
            manager = HashChainManager(state_file)

            entries = []
            for i in range(5):
                entry = manager.add_integrity({"event": f"event_{i}"})
                entries.append(entry)

            verifier = HashChainVerifier()
            is_valid, error = verifier.verify_chain(entries)

            assert is_valid
            assert error is None

    def test_verify_chain_detects_modification(self):
        """Test that modified entries are detected."""
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "state.json"
            manager = HashChainManager(state_file)

            entries = []
            for i in range(3):
                entry = manager.add_integrity({"event": f"event_{i}"})
                entries.append(entry)

            # Tamper with middle entry
            entries[1]["event"] = "TAMPERED"

            verifier = HashChainVerifier()
            is_valid, error = verifier.verify_chain(entries)

            assert not is_valid
            assert error is not None
            assert "mismatch" in error.lower() or "modified" in error.lower()

    def test_verify_chain_detects_deletion(self):
        """Test that deleted entries are detected."""
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "state.json"
            manager = HashChainManager(state_file)

            entries = []
            for i in range(5):
                entry = manager.add_integrity({"event": f"event_{i}"})
                entries.append(entry)

            # Remove middle entry
            entries.pop(2)

            verifier = HashChainVerifier()
            is_valid, error = verifier.verify_chain(entries)

            assert not is_valid
            # Should detect sequence gap or chain break
            assert error is not None

    def test_verify_chain_detects_reordering(self):
        """Test that reordered entries are detected."""
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "state.json"
            manager = HashChainManager(state_file)

            entries = []
            for i in range(3):
                entry = manager.add_integrity({"event": f"event_{i}"})
                entries.append(entry)

            # Swap entries
            entries[1], entries[2] = entries[2], entries[1]

            verifier = HashChainVerifier()
            is_valid, error = verifier.verify_chain(entries)

            assert not is_valid

    def test_find_tampering(self):
        """Test find_tampering method."""
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "state.json"
            manager = HashChainManager(state_file)

            entries = []
            for i in range(3):
                entry = manager.add_integrity({"event": f"event_{i}"})
                entries.append(entry)

            # Tamper with middle entry
            entries[1]["event"] = "TAMPERED"

            verifier = HashChainVerifier()
            issues = verifier.find_tampering(entries)

            assert len(issues) > 0
            assert any(i["type"] == "entry_modified" for i in issues)


class TestVerifyAuditLogIntegrity:
    """Tests for file-level integrity verification."""

    def test_verify_valid_log_file(self):
        """Test verification of valid log file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "state.json"
            log_file = Path(tmpdir) / "audit.jsonl"

            manager = HashChainManager(state_file)

            # Write entries to file
            with open(log_file, "w") as f:
                for i in range(5):
                    entry = manager.add_integrity({"event": f"event_{i}"})
                    f.write(json.dumps(entry) + "\n")

            is_valid, issues = verify_audit_log_integrity(log_file)

            assert is_valid
            assert issues is None or len(issues) == 0

    def test_verify_tampered_log_file(self):
        """Test detection of tampered log file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "state.json"
            log_file = Path(tmpdir) / "audit.jsonl"

            manager = HashChainManager(state_file)

            # Write entries to file
            entries = []
            for i in range(5):
                entry = manager.add_integrity({"event": f"event_{i}"})
                entries.append(entry)

            # Tamper with one entry before writing
            entries[2]["event"] = "TAMPERED"

            with open(log_file, "w") as f:
                for entry in entries:
                    f.write(json.dumps(entry) + "\n")

            is_valid, issues = verify_audit_log_integrity(log_file)

            assert not is_valid

    def test_verify_empty_file(self):
        """Test verification of empty file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = Path(tmpdir) / "empty.jsonl"
            log_file.touch()

            is_valid, issues = verify_audit_log_integrity(log_file)

            # Empty file should be valid (nothing to verify)
            assert is_valid
