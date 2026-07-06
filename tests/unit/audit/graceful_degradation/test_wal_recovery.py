"""
HashChainWALRecovery 테스트.

WAL-based crash recovery 테스트.
"""

import json


class TestHashChainWALRecovery:
    """Tests for HashChainWALRecovery."""

    def test_initialization(self, temp_dir):
        """Test WAL recovery initialization."""
        from baldur.audit.graceful_degradation import HashChainWALRecovery

        recovery = HashChainWALRecovery(wal_dir=temp_dir)

        assert recovery._wal_dir == temp_dir
        assert recovery._recovery_done is False

    def test_write_wal_entry(self, temp_dir, sample_entry):
        """Test writing entry to WAL."""
        from baldur.audit.graceful_degradation import HashChainWALRecovery

        recovery = HashChainWALRecovery(wal_dir=temp_dir)

        wal_seq = recovery.write_wal_entry("add_integrity", sample_entry)

        assert wal_seq == 1

        # Verify file was written
        wal_files = list(temp_dir.glob("hash_chain_wal_*.jsonl"))
        assert len(wal_files) == 1

        with open(wal_files[0]) as f:
            content = f.read()
            assert "add_integrity" in content

        recovery.close()

    def test_mark_wal_committed(self, temp_dir, sample_entry):
        """Test marking WAL entry as committed."""
        from baldur.audit.graceful_degradation import HashChainWALRecovery

        recovery = HashChainWALRecovery(wal_dir=temp_dir)

        wal_seq = recovery.write_wal_entry("add_integrity", sample_entry)
        recovery.mark_wal_committed(wal_seq)

        # Read and verify commit marker
        wal_files = list(temp_dir.glob("hash_chain_wal_*.jsonl"))
        with open(wal_files[0]) as f:
            lines = f.readlines()

        assert len(lines) == 2
        commit_entry = json.loads(lines[1])
        assert commit_entry["operation"] == "COMMIT"

        recovery.close()

    def test_recover_on_startup_empty(self, temp_dir, mock_redis):
        """Test recovery with no WAL files."""
        from baldur.audit.graceful_degradation import HashChainWALRecovery

        recovery = HashChainWALRecovery(
            wal_dir=temp_dir,
            redis_client=mock_redis,
        )

        result = recovery.recover_on_startup()

        assert result["status"] == "success"
        assert result["wal_files_scanned"] == 0
        assert recovery._recovery_done is True

    def test_recover_uncommitted_entries(self, temp_dir, mock_redis):
        """Test recovery replays uncommitted entries."""
        from unittest.mock import patch

        from baldur.audit.graceful_degradation import HashChainWALRecovery

        recovery = HashChainWALRecovery(
            wal_dir=temp_dir,
            redis_client=mock_redis,
        )

        # Write entry without commit
        entry = {"integrity": {"sequence": 5, "current_hash": "abc123"}}
        recovery.write_wal_entry("add_integrity", entry)

        # Don't commit - simulate crash
        recovery.close()

        # Create new recovery instance
        recovery2 = HashChainWALRecovery(
            wal_dir=temp_dir,
            redis_client=mock_redis,
        )

        # Mock batch idempotency to avoid cross-test state pollution
        with patch.object(recovery2, "_batch_check_idempotency", return_value=set()):
            result = recovery2.recover_on_startup()

        assert result["status"] == "success"
        assert result["entries_found"] == 1
        assert result["entries_recovered"] == 1

        recovery2.close()

    def test_skip_committed_entries(self, temp_dir, mock_redis):
        """Test recovery skips already committed entries."""
        from baldur.audit.graceful_degradation import HashChainWALRecovery

        recovery = HashChainWALRecovery(
            wal_dir=temp_dir,
            redis_client=mock_redis,
        )

        # Write and commit entry
        entry = {"integrity": {"sequence": 5, "current_hash": "abc123"}}
        wal_seq = recovery.write_wal_entry("add_integrity", entry)
        recovery.mark_wal_committed(wal_seq)
        recovery.close()

        # Create new recovery instance
        recovery2 = HashChainWALRecovery(
            wal_dir=temp_dir,
            redis_client=mock_redis,
        )

        result = recovery2.recover_on_startup()

        assert result["entries_already_committed"] == 1
        assert result["entries_recovered"] == 0

        recovery2.close()

    def test_cleanup_old_wal_files(self, temp_dir):
        """Test cleanup of old WAL files."""
        from baldur.audit.graceful_degradation import HashChainWALRecovery

        # Create old WAL file
        old_file = temp_dir / "hash_chain_wal_20200101.jsonl"
        old_file.write_text('{"test": true}\n')

        recovery = HashChainWALRecovery(wal_dir=temp_dir)
        removed = recovery.cleanup_old_wal_files(max_age_days=1)

        assert removed == 1
        assert not old_file.exists()

    def test_get_stats(self, temp_dir, sample_entry):
        """Test statistics tracking."""
        from baldur.audit.graceful_degradation import HashChainWALRecovery

        recovery = HashChainWALRecovery(wal_dir=temp_dir)
        recovery.write_wal_entry("test", sample_entry)

        stats = recovery.get_stats()

        assert stats["wal_sequence"] == 1
        assert stats["recovery_done"] is False

        recovery.close()
