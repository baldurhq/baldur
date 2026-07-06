"""
크래시 복구 시나리오 Chaos 테스트.

프로세스 크래시 복구 능력 검증:
- WAL 기반 미커밋 쓰기 복구
- 크래시 후 PENDING 시퀀스 정리
- 파일에서 해시 체인 상태 복원
- 크래시 복구 후 데이터 무결성

실제 크래시 시나리오 시뮬레이션:
- 쓰기 중간 프로세스 크래시
- 미처리 데이터가 있는 서버 재시작
- WAL 엔트리에서 복구

Related code:
    baldur/audit/graceful_degradation.py#HashChainWALRecovery
    baldur/audit/integrity.py#StartupHashChainSync
    baldur/audit/integrity.py#PendingSequenceManager
"""

from __future__ import annotations

import json
import tempfile
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from baldur.audit.graceful_degradation import (
    HashChainWALRecovery,
)
from baldur.audit.integrity import (
    PendingSequenceManager,
    StartupHashChainSync,
    compute_hash,
)

# =============================================================================
# Mock Redis for Crash Tests
# =============================================================================


class CrashTestRedis:
    """
    Redis client for crash recovery testing.

    Can simulate crash scenarios:
    - Complete data loss (Redis restart)
    - Partial data (some keys lost)
    - Stale PENDING keys (from crashed process)
    """

    def __init__(self):
        self._data: dict[str, Any] = {}
        self._hashes: dict[str, dict[str, str]] = {}
        self._lock = threading.Lock()

    def clear_all(self):
        """Simulate Redis restart (data loss)."""
        self._data.clear()
        self._hashes.clear()

    def clear_chain_state(self):
        """Clear only chain state keys (partial loss)."""
        keys_to_delete = [k for k in self._data if "hash_chain" in k]
        for k in keys_to_delete:
            del self._data[k]
        keys_to_delete = [k for k in self._hashes if "hash_chain" in k]
        for k in keys_to_delete:
            del self._hashes[k]

    def get(self, key: str) -> bytes | None:
        value = self._data.get(key)
        return str(value).encode() if value is not None else None

    def set(self, key: str, value: Any, nx: bool = False, ex: int = None) -> bool:
        with self._lock:
            if nx and key in self._data:
                return False
            self._data[key] = value
            return True

    def delete(self, *keys: str) -> int:
        count = 0
        for key in keys:
            if key in self._data:
                del self._data[key]
                count += 1
            if key in self._hashes:
                del self._hashes[key]
                count += 1
        return count

    def keys(self, pattern: str) -> list[bytes]:
        import fnmatch

        all_keys = list(self._data.keys()) + list(self._hashes.keys())
        return [k.encode() for k in all_keys if fnmatch.fnmatch(k, pattern)]

    def hget(self, key: str, field: str) -> bytes | None:
        hash_data = self._hashes.get(key, {})
        value = hash_data.get(field)
        return str(value).encode() if value is not None else None

    def hset(self, key: str, mapping: dict[str, Any] = None, **kwargs) -> int:
        if mapping is None:
            mapping = kwargs
        with self._lock:
            if key not in self._hashes:
                self._hashes[key] = {}
            self._hashes[key].update({str(k): str(v) for k, v in mapping.items()})
            return len(mapping)

    def hgetall(self, key: str) -> dict[bytes, bytes]:
        hash_data = self._hashes.get(key, {})
        return {k.encode(): v.encode() for k, v in hash_data.items()}

    def incr(self, key: str) -> int:
        with self._lock:
            current = int(self._data.get(key, 0))
            new_value = current + 1
            self._data[key] = new_value
            return new_value

    def expire(self, key: str, seconds: int) -> int:
        return 1

    def pipeline(self, transaction: bool = True) -> MockPipeline:
        return MockPipeline(self)


class MockPipeline:
    """Mock Redis pipeline."""

    def __init__(self, redis: CrashTestRedis):
        self._redis = redis
        self._commands: list[tuple] = []

    def set(self, key: str, value: Any, ex: int = None) -> MockPipeline:
        """ex parameter is accepted but ignored in mock."""
        self._commands.append(("set", key, value))
        return self

    def hset(self, key: str, mapping: dict = None, **kwargs) -> MockPipeline:
        self._commands.append(("hset", key, mapping or kwargs))
        return self

    def delete(self, *keys) -> MockPipeline:
        self._commands.append(("delete", keys))
        return self

    def execute(self) -> list[Any]:
        results = []
        for cmd in self._commands:
            if cmd[0] == "set":
                self._redis.set(cmd[1], cmd[2])
                results.append(True)
            elif cmd[0] == "hset":
                self._redis.hset(cmd[1], cmd[2])
                results.append(1)
            elif cmd[0] == "delete":
                for key in cmd[1]:
                    self._redis.delete(key)
                results.append(len(cmd[1]))
        return results


# =============================================================================
# Test Fixtures
# =============================================================================


@pytest.fixture
def crash_redis():
    """Create a crash test Redis client."""
    return CrashTestRedis()


@pytest.fixture
def temp_wal_dir():
    """Create temp directory for WAL files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def temp_log_dir():
    """Create temp directory for audit logs."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


def create_wal_entry(
    sequence: int,
    entry_data: dict[str, Any],
    committed: bool = False,
) -> dict[str, Any]:
    """Create a WAL entry dictionary for testing."""
    return {
        "wal_sequence": sequence,
        "operation": "add_integrity",
        "entry_data": entry_data,
        "timestamp": datetime.now(UTC).isoformat(),
        "pod_id": "test-pod",
        "committed": committed,
    }


def write_wal_file(
    wal_dir: Path,
    entries: list[dict[str, Any]],
) -> Path:
    """Write WAL entries to file."""
    date_str = datetime.now(UTC).strftime("%Y%m%d")
    wal_file = wal_dir / f"hash_chain_wal_{date_str}.jsonl"

    with open(wal_file, "w", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")

    return wal_file


def create_audit_entry(sequence: int, previous_hash: str = "GENESIS") -> dict[str, Any]:
    """Create an audit log entry."""
    entry = {
        "event": f"test_event_{sequence}",
        "data": {"seq": sequence},
        "integrity": {
            "sequence": sequence,
            "previous_hash": previous_hash,
            "timestamp": datetime.now(UTC).isoformat(),
            "pod_id": "test-pod",
        },
    }
    current_hash = compute_hash(entry)
    entry["integrity"]["current_hash"] = current_hash
    return entry


def write_audit_file(log_dir: Path, entries: list[dict[str, Any]]) -> Path:
    """Write audit entries to file."""
    date = datetime.now(UTC).strftime("%Y%m%d")
    log_file = log_dir / f"audit_{date}.jsonl"

    with open(log_file, "w", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(entry, default=str) + "\n")

    return log_file


# =============================================================================
# Test: WAL-Based Recovery
# =============================================================================


class TestWALBasedRecovery:
    """Tests for WAL-based crash recovery."""

    def test_recover_uncommitted_wal_entries(self, crash_redis, temp_wal_dir):
        """
        Uncommitted WAL entries should be recovered on startup.
        """
        # Create WAL with uncommitted entries
        entries = [
            create_wal_entry(1, create_audit_entry(1), committed=False),
            create_wal_entry(2, create_audit_entry(2), committed=False),
        ]
        write_wal_file(temp_wal_dir, entries)

        recovery = HashChainWALRecovery(
            wal_dir=temp_wal_dir,
        )

        result = recovery.recover_on_startup()

        assert result["status"] == "success"
        assert result["entries_found"] == 2

    def test_skip_committed_wal_entries(self, crash_redis, temp_wal_dir):
        """
        Committed WAL entries should be skipped during recovery.
        """
        # Create entries and then commit markers
        entries = [
            create_wal_entry(1, create_audit_entry(1), committed=False),
            {
                "wal_sequence": 1,
                "operation": "COMMIT",
                "timestamp": datetime.now(UTC).isoformat(),
            },
            create_wal_entry(2, create_audit_entry(2), committed=False),
            {
                "wal_sequence": 2,
                "operation": "COMMIT",
                "timestamp": datetime.now(UTC).isoformat(),
            },
            create_wal_entry(3, create_audit_entry(3), committed=False),
        ]
        write_wal_file(temp_wal_dir, entries)

        recovery = HashChainWALRecovery(
            wal_dir=temp_wal_dir,
        )

        result = recovery.recover_on_startup()

        # Entry 3 is not committed, 1 and 2 are
        assert result["entries_found"] == 3
        assert result["entries_already_committed"] == 2

    def test_empty_wal_recovery(self, temp_wal_dir):
        """
        Empty or non-existent WAL should not cause errors.
        """
        recovery = HashChainWALRecovery(
            wal_dir=temp_wal_dir,
        )

        result = recovery.recover_on_startup()

        assert result["status"] == "success"
        assert result["entries_found"] == 0

    def test_corrupted_wal_handled_gracefully(self, temp_wal_dir):
        """
        Corrupted WAL entries should be skipped without crash.
        """
        date_str = datetime.now(UTC).strftime("%Y%m%d")
        wal_file = temp_wal_dir / f"hash_chain_wal_{date_str}.jsonl"

        with open(wal_file, "w") as f:
            f.write("invalid json\n")
            f.write(
                json.dumps(
                    {
                        "wal_sequence": 1,
                        "operation": "add_integrity",
                        "entry_data": create_audit_entry(1),
                        "timestamp": datetime.now(UTC).isoformat(),
                        "pod_id": "test",
                        "committed": False,
                    }
                )
                + "\n"
            )
            f.write("{incomplete\n")

        recovery = HashChainWALRecovery(
            wal_dir=temp_wal_dir,
        )

        result = recovery.recover_on_startup()

        # Should still process valid entry
        assert result["status"] == "success"
        assert result["entries_found"] == 1


# =============================================================================
# Test: PENDING Sequence Recovery
# =============================================================================


class TestPendingSequenceRecovery:
    """Tests for recovery of PENDING sequences after crash."""

    def test_stale_pending_detected_on_startup(self, crash_redis, temp_log_dir):
        """
        Stale PENDING sequences from crashed process should be detected.
        """
        # Simulate crashed process that left PENDING
        crash_redis.set("test:audit:hash_chain:pending:11", "expected_hash_11")
        crash_redis.set("test:audit:hash_chain:pending:12", "expected_hash_12")

        # Set up normal chain state
        crash_redis.set("test:audit:hash_chain:seq", 10)
        crash_redis.hset(
            "test:audit:hash_chain:state",
            mapping={
                "sequence": "10",
                "previous_hash": "hash_at_10",
            },
        )

        sync = StartupHashChainSync(
            redis_client=crash_redis,
            log_dir=temp_log_dir,
            key_prefix="test:",
        )

        result = sync.sync()

        # Should have cleaned up PENDING
        assert result["status"] == "success"
        assert result["pending_cleaned"] >= 0

    def test_pending_sequence_reserve_and_commit(self, crash_redis):
        """
        Normal PENDING lifecycle: reserve -> commit.
        """
        manager = PendingSequenceManager(
            redis_client=crash_redis,
            key_prefix="test:",
        )

        # Reserve
        assert manager.reserve_sequence(1, "expected_hash")

        # Verify PENDING exists
        pending = crash_redis.get("test:audit:hash_chain:pending:1")
        assert pending is not None

        # Commit
        assert manager.commit_sequence(1)

        # Verify PENDING removed
        pending = crash_redis.get("test:audit:hash_chain:pending:1")
        assert pending is None

    def test_pending_sequence_abort_moves_to_orphaned(self, crash_redis):
        """
        Failed write should move PENDING to ORPHANED.
        """
        manager = PendingSequenceManager(
            redis_client=crash_redis,
            key_prefix="test:",
        )

        # Reserve
        manager.reserve_sequence(1, "expected_hash")

        # Abort (simulates file write failure)
        manager.abort_sequence(1)

        # Verify PENDING removed
        pending = crash_redis.get("test:audit:hash_chain:pending:1")
        assert pending is None

        # Verify ORPHANED created
        orphaned = crash_redis.get("test:audit:hash_chain:orphaned:1")
        assert orphaned is not None


# =============================================================================
# Test: Hash Chain State Restoration
# =============================================================================


class TestHashChainStateRestoration:
    """Tests for restoring hash chain state from files."""

    def test_restore_state_from_file_after_redis_loss(self, crash_redis, temp_log_dir):
        """
        After Redis data loss, state should be restored from files.
        """
        # Create audit log file
        entries = []
        prev_hash = "GENESIS"
        for i in range(5):
            entry = create_audit_entry(i + 1, prev_hash)
            entries.append(entry)
            prev_hash = entry["integrity"]["current_hash"]

        write_audit_file(temp_log_dir, entries)

        # Redis has no state (simulates data loss)
        # crash_redis is empty

        sync = StartupHashChainSync(
            redis_client=crash_redis,
            log_dir=temp_log_dir,
            key_prefix="test:",
        )

        result = sync.sync()

        assert result["status"] == "success"
        assert result["action"] == "synced_redis_to_file"
        assert result["file_sequence"] == 5

        # Verify Redis was restored
        assert int(crash_redis.get("test:audit:hash_chain:seq")) == 5

    def test_restore_maintains_chain_integrity(self, crash_redis, temp_log_dir):
        """
        Restored state should maintain valid chain linkage.
        """
        # Create valid chain
        entries = []
        prev_hash = "GENESIS"
        for i in range(3):
            entry = create_audit_entry(i + 1, prev_hash)
            entries.append(entry)
            prev_hash = entry["integrity"]["current_hash"]

        write_audit_file(temp_log_dir, entries)

        sync = StartupHashChainSync(
            redis_client=crash_redis,
            log_dir=temp_log_dir,
            key_prefix="test:",
        )

        sync.sync()

        # Verify last hash matches
        state = crash_redis.hgetall("test:audit:hash_chain:state")
        restored_hash = state[b"previous_hash"].decode()

        expected_hash = entries[-1]["integrity"]["current_hash"]
        assert restored_hash == expected_hash


# =============================================================================
# Test: Mid-Write Crash Scenarios
# =============================================================================


class TestMidWriteCrashScenarios:
    """Tests simulating crashes at various points during write."""

    def test_crash_after_redis_update_before_file(self, crash_redis, temp_log_dir):
        """
        Crash after Redis update but before file write.

        Expected: PENDING key left, cleanup on restart
        """
        # Simulate state after Redis update
        crash_redis.set("test:audit:hash_chain:seq", 6)
        crash_redis.hset(
            "test:audit:hash_chain:state",
            mapping={
                "sequence": "6",
                "previous_hash": "new_hash",
            },
        )
        crash_redis.set("test:audit:hash_chain:pending:6", "expected_hash_6")

        # File only has 5 entries
        entries = []
        prev_hash = "GENESIS"
        for i in range(5):
            entry = create_audit_entry(i + 1, prev_hash)
            entries.append(entry)
            prev_hash = entry["integrity"]["current_hash"]

        write_audit_file(temp_log_dir, entries)

        # Startup sync
        sync = StartupHashChainSync(
            redis_client=crash_redis,
            log_dir=temp_log_dir,
            key_prefix="test:",
        )

        result = sync.sync()

        # Should detect Redis ahead and clean PENDING
        assert result["status"] == "success"

    def test_crash_before_redis_update(self, crash_redis, temp_log_dir):
        """
        Crash before any Redis update.

        Expected: No PENDING, clean state
        """
        # Set up initial state
        entries = []
        prev_hash = "GENESIS"
        for i in range(5):
            entry = create_audit_entry(i + 1, prev_hash)
            entries.append(entry)
            prev_hash = entry["integrity"]["current_hash"]

        write_audit_file(temp_log_dir, entries)

        crash_redis.set("test:audit:hash_chain:seq", 5)
        crash_redis.hset(
            "test:audit:hash_chain:state",
            mapping={
                "sequence": "5",
                "previous_hash": entries[-1]["integrity"]["current_hash"],
            },
        )

        sync = StartupHashChainSync(
            redis_client=crash_redis,
            log_dir=temp_log_dir,
            key_prefix="test:",
        )

        result = sync.sync()

        # Should be in sync
        assert result["status"] == "success"
        assert result["action"] == "in_sync"


# =============================================================================
# Test: Multiple Crashes and Recovery Cycles
# =============================================================================


class TestMultipleCrashCycles:
    """Tests for system stability across multiple crash/recovery cycles."""

    def test_multiple_startup_syncs_idempotent(self, crash_redis, temp_log_dir):
        """
        Multiple sync calls should be safe and idempotent.
        """
        entries = []
        prev_hash = "GENESIS"
        for i in range(5):
            entry = create_audit_entry(i + 1, prev_hash)
            entries.append(entry)
            prev_hash = entry["integrity"]["current_hash"]

        write_audit_file(temp_log_dir, entries)

        # First sync
        sync1 = StartupHashChainSync(
            redis_client=crash_redis,
            log_dir=temp_log_dir,
            key_prefix="test:",
        )
        result1 = sync1.sync()

        # Simulate restart with new sync instance
        sync2 = StartupHashChainSync(
            redis_client=crash_redis,
            log_dir=temp_log_dir,
            key_prefix="test:",
        )
        result2 = sync2.sync()

        # Both should succeed, second should detect in_sync
        assert result1["status"] == "success"
        assert result2["status"] == "success"
        assert result2["action"] == "in_sync"

    def test_sequence_continuity_after_crashes(self, crash_redis, temp_log_dir):
        """
        Sequence numbers should remain continuous after crashes.
        """
        # Initial state with 5 entries
        entries = []
        prev_hash = "GENESIS"
        for i in range(5):
            entry = create_audit_entry(i + 1, prev_hash)
            entries.append(entry)
            prev_hash = entry["integrity"]["current_hash"]

        write_audit_file(temp_log_dir, entries)

        # First sync
        sync = StartupHashChainSync(
            redis_client=crash_redis,
            log_dir=temp_log_dir,
            key_prefix="test:",
        )
        sync.sync()

        # Verify sequence
        seq = int(crash_redis.get("test:audit:hash_chain:seq"))
        assert seq == 5

        # Add more entries
        new_entry = create_audit_entry(6, entries[-1]["integrity"]["current_hash"])
        entries.append(new_entry)
        write_audit_file(temp_log_dir, entries)

        # Simulate crash and new sync
        sync2 = StartupHashChainSync(
            redis_client=crash_redis,
            log_dir=temp_log_dir,
            key_prefix="test:",
        )
        result = sync2.sync()

        # File ahead, should sync
        assert result["file_sequence"] == 6


# =============================================================================
# Test: Data Integrity After Recovery
# =============================================================================


class TestDataIntegrityAfterRecovery:
    """Tests ensuring data integrity is maintained after recovery."""

    def test_hash_chain_valid_after_recovery(self, crash_redis, temp_log_dir):
        """
        Hash chain should remain valid after crash recovery.
        """
        # Create valid chain
        entries = []
        prev_hash = "GENESIS"
        for i in range(5):
            entry = create_audit_entry(i + 1, prev_hash)
            entries.append(entry)
            prev_hash = entry["integrity"]["current_hash"]

        write_audit_file(temp_log_dir, entries)

        # Recovery
        sync = StartupHashChainSync(
            redis_client=crash_redis,
            log_dir=temp_log_dir,
            key_prefix="test:",
        )
        sync.sync()

        # Verify Redis state matches file
        state = crash_redis.hgetall("test:audit:hash_chain:state")
        redis_hash = state[b"previous_hash"].decode()
        redis_seq = int(state[b"sequence"])

        assert redis_seq == 5
        assert redis_hash == entries[-1]["integrity"]["current_hash"]

    def test_no_duplicate_sequences_after_recovery(self, crash_redis, temp_log_dir):
        """
        Recovery should not create duplicate sequence numbers.
        """
        # Set Redis ahead
        crash_redis.set("test:audit:hash_chain:seq", 10)
        crash_redis.hset(
            "test:audit:hash_chain:state",
            mapping={
                "sequence": "10",
                "previous_hash": "some_hash",
            },
        )

        # File has fewer
        entries = []
        prev_hash = "GENESIS"
        for i in range(5):
            entry = create_audit_entry(i + 1, prev_hash)
            entries.append(entry)
            prev_hash = entry["integrity"]["current_hash"]

        write_audit_file(temp_log_dir, entries)

        sync = StartupHashChainSync(
            redis_client=crash_redis,
            log_dir=temp_log_dir,
            key_prefix="test:",
        )
        result = sync.sync()

        # Redis ahead is normal, sequence should stay at 10
        assert result["action"] == "redis_ahead_ok"
        seq = int(crash_redis.get("test:audit:hash_chain:seq"))
        assert seq == 10


# =============================================================================
# Test: Recovery Statistics
# =============================================================================


class TestRecoveryStatistics:
    """Tests for recovery statistics and logging."""

    def test_recovery_returns_statistics(self, crash_redis, temp_log_dir):
        """
        Recovery should return detailed statistics.
        """
        entries = []
        prev_hash = "GENESIS"
        for i in range(5):
            entry = create_audit_entry(i + 1, prev_hash)
            entries.append(entry)
            prev_hash = entry["integrity"]["current_hash"]

        write_audit_file(temp_log_dir, entries)

        sync = StartupHashChainSync(
            redis_client=crash_redis,
            log_dir=temp_log_dir,
            key_prefix="test:",
        )

        result = sync.sync()

        # Should have all expected fields
        assert "status" in result
        assert "file_sequence" in result
        assert "redis_sequence" in result
        assert "action" in result
        assert "synced_at" in result

    def test_wal_recovery_returns_statistics(self, temp_wal_dir):
        """
        WAL recovery should return statistics.
        """
        entries = [
            create_wal_entry(1, create_audit_entry(1), committed=False),
            {
                "wal_sequence": 1,
                "operation": "COMMIT",
                "timestamp": datetime.now(UTC).isoformat(),
            },
            create_wal_entry(2, create_audit_entry(2), committed=False),
        ]
        write_wal_file(temp_wal_dir, entries)

        recovery = HashChainWALRecovery(wal_dir=temp_wal_dir)
        result = recovery.recover_on_startup()

        assert "status" in result
        assert "entries_found" in result
        assert result["entries_found"] == 2
        assert result["entries_already_committed"] == 1
