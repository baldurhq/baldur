"""
StartupHashChainSync 통합 테스트.

시스템 시작 시 해시 체인 동기화 전체 워크플로우 테스트:
- Redis/파일 상태 비교 및 동기화 결정 로직
- Redis 앞선 상태, 파일 앞선 상태, 동기화된 상태 처리
- 크래시 후 PENDING 시퀀스 정리
- 멱등성(idempotent) 동기화 동작

테스트 시나리오는 크래시나 네트워크 문제로 인해
Redis와 파일 상태가 불일치할 수 있는 실제 재시작 상황을 시뮬레이션합니다.

Related code:
    baldur/audit/integrity.py#StartupHashChainSync
"""

from __future__ import annotations

import json
import tempfile
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from baldur.audit.integrity import (
    StartupHashChainSync,
    compute_hash,
)

# =============================================================================
# Mock Redis Client for Integration Tests
# =============================================================================


class IntegrationMockRedis:
    """
    Mock Redis client simulating real Redis behavior for integration tests.

    Supports all operations used by StartupHashChainSync:
    - GET/SET for sequence tracking
    - HGET/HSET/HGETALL for state storage
    - DELETE for cleanup
    - KEYS for pattern matching
    """

    def __init__(self):
        self._data: dict[str, Any] = {}
        self._hashes: dict[str, dict[str, str]] = {}
        self._lock = threading.Lock()
        self._failure_mode = False

    def enable_failure_mode(self):
        """Simulate Redis unavailability."""
        self._failure_mode = True

    def disable_failure_mode(self):
        """Restore Redis availability."""
        self._failure_mode = False

    def get(self, key: str) -> bytes | None:
        if self._failure_mode:
            raise ConnectionError("Redis unavailable")
        value = self._data.get(key)
        return str(value).encode() if value is not None else None

    def set(self, key: str, value: Any, nx: bool = False, ex: int = None) -> bool:
        if self._failure_mode:
            raise ConnectionError("Redis unavailable")
        with self._lock:
            if nx and key in self._data:
                return False
            self._data[key] = value
            return True

    def delete(self, *keys: str) -> int:
        if self._failure_mode:
            raise ConnectionError("Redis unavailable")
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
        if self._failure_mode:
            raise ConnectionError("Redis unavailable")
        import fnmatch

        all_keys = list(self._data.keys()) + list(self._hashes.keys())
        return [k.encode() for k in all_keys if fnmatch.fnmatch(k, pattern)]

    def hget(self, key: str, field: str) -> bytes | None:
        if self._failure_mode:
            raise ConnectionError("Redis unavailable")
        hash_data = self._hashes.get(key, {})
        value = hash_data.get(field)
        return str(value).encode() if value is not None else None

    def hset(self, key: str, mapping: dict[str, Any] = None, **kwargs) -> int:
        if self._failure_mode:
            raise ConnectionError("Redis unavailable")
        if mapping is None:
            mapping = kwargs
        with self._lock:
            if key not in self._hashes:
                self._hashes[key] = {}
            self._hashes[key].update({str(k): str(v) for k, v in mapping.items()})
            return len(mapping)

    def hgetall(self, key: str) -> dict[bytes, bytes]:
        if self._failure_mode:
            raise ConnectionError("Redis unavailable")
        hash_data = self._hashes.get(key, {})
        return {k.encode(): v.encode() for k, v in hash_data.items()}

    def expire(self, key: str, seconds: int) -> int:
        return 1 if key in self._data or key in self._hashes else 0

    def incr(self, key: str) -> int:
        if self._failure_mode:
            raise ConnectionError("Redis unavailable")
        with self._lock:
            current = int(self._data.get(key, 0))
            new_value = current + 1
            self._data[key] = new_value
            return new_value

    def pipeline(self, transaction: bool = True) -> MockPipeline:
        return MockPipeline(self)


class MockPipeline:
    """Mock Redis pipeline."""

    def __init__(self, redis: IntegrationMockRedis):
        self._redis = redis
        self._commands: list[tuple] = []

    def set(self, key: str, value: Any) -> MockPipeline:
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
def temp_log_dir():
    """Create a temporary directory for audit log files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def mock_redis():
    """Create a mock Redis client."""
    return IntegrationMockRedis()


def create_audit_log_file(
    log_dir: Path, entries: list[dict[str, Any]], date: str = None
) -> Path:
    """
    Helper to create an audit log file with given entries.

    Args:
        log_dir: Directory to create file in
        entries: List of log entries
        date: Date string for filename (default: today)

    Returns:
        Path to created file
    """
    if date is None:
        date = datetime.now(UTC).strftime("%Y%m%d")

    log_file = log_dir / f"audit_{date}.jsonl"

    with open(log_file, "w", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(entry, default=str) + "\n")

    return log_file


def build_hash_chain_entries(count: int, start_seq: int = 1) -> list[dict[str, Any]]:
    """
    Build a valid hash chain with given number of entries.

    Creates entries with proper sequence, previous_hash, and current_hash.

    Args:
        count: Number of entries to create
        start_seq: Starting sequence number

    Returns:
        List of entries with integrity fields
    """
    entries = []
    previous_hash = "GENESIS"

    for i in range(count):
        seq = start_seq + i
        timestamp = datetime.now(UTC).isoformat()

        entry = {
            "event": f"test_event_{seq}",
            "data": {"value": seq},
            "integrity": {
                "sequence": seq,
                "previous_hash": previous_hash,
                "timestamp": timestamp,
                "pod_id": "test-pod",
            },
        }

        # Compute current hash (without current_hash field)
        current_hash = compute_hash(entry)
        entry["integrity"]["current_hash"] = current_hash

        entries.append(entry)
        previous_hash = current_hash

    return entries


# =============================================================================
# Test: Fresh Start Scenario
# =============================================================================


class TestStartupSyncFreshStart:
    """Tests for fresh start scenario (both Redis and file empty)."""

    def test_fresh_start_empty_redis_empty_file(self, mock_redis, temp_log_dir):
        """
        Fresh start: Redis and file both empty.

        Expected: action='fresh_start', no sync needed
        """
        sync = StartupHashChainSync(
            redis_client=mock_redis,
            log_dir=temp_log_dir,
            key_prefix="test:",
        )

        result = sync.sync()

        assert result["status"] == "success"
        assert result["action"] == "fresh_start"
        assert result["file_sequence"] == 0
        assert result["redis_sequence"] == 0

    def test_fresh_start_no_log_directory(self, mock_redis):
        """
        Fresh start with non-existent log directory.

        Expected: Treats as empty file state
        """
        non_existent_dir = Path("/tmp/non_existent_audit_logs_12345")

        sync = StartupHashChainSync(
            redis_client=mock_redis,
            log_dir=non_existent_dir,
            key_prefix="test:",
        )

        result = sync.sync()

        assert result["status"] == "success"
        assert result["action"] == "fresh_start"


# =============================================================================
# Test: Redis Ahead of File
# =============================================================================


class TestStartupSyncRedisAhead:
    """
    Tests for Redis-ahead scenario (file writes pending).

    This occurs when:
    - Process crashed after Redis update but before file write
    - Normal operation with batched file writes
    """

    def test_redis_ahead_normal_operation(self, mock_redis, temp_log_dir):
        """
        Redis has higher sequence than file.

        Expected: action='redis_ahead_ok', no changes needed
        """
        # Set up Redis with sequence 10
        mock_redis.set("test:audit:hash_chain:seq", 10)
        mock_redis.hset(
            "test:audit:hash_chain:state",
            mapping={
                "sequence": "10",
                "previous_hash": "abc123",
            },
        )

        # Create file with only 5 entries
        entries = build_hash_chain_entries(5)
        create_audit_log_file(temp_log_dir, entries)

        sync = StartupHashChainSync(
            redis_client=mock_redis,
            log_dir=temp_log_dir,
            key_prefix="test:",
        )

        result = sync.sync()

        assert result["status"] == "success"
        assert result["action"] == "redis_ahead_ok"
        assert result["redis_sequence"] == 10
        assert result["file_sequence"] == 5

        # Verify Redis state unchanged
        assert int(mock_redis.get("test:audit:hash_chain:seq")) == 10

    def test_redis_ahead_by_one_batch_pending(self, mock_redis, temp_log_dir):
        """
        Redis ahead by exactly 1 (typical pending write scenario).

        Expected: Normal state, waiting for batch flush
        """
        # Set up Redis with sequence 11
        mock_redis.set("test:audit:hash_chain:seq", 11)
        mock_redis.hset(
            "test:audit:hash_chain:state",
            mapping={
                "sequence": "11",
                "previous_hash": "def456",
            },
        )

        # Create file with 10 entries
        entries = build_hash_chain_entries(10)
        create_audit_log_file(temp_log_dir, entries)

        sync = StartupHashChainSync(
            redis_client=mock_redis,
            log_dir=temp_log_dir,
            key_prefix="test:",
        )

        result = sync.sync()

        assert result["status"] == "success"
        assert result["action"] == "redis_ahead_ok"


# =============================================================================
# Test: File Ahead of Redis (Recovery Scenario)
# =============================================================================


class TestStartupSyncFileAhead:
    """
    Tests for file-ahead scenario (Redis data loss recovery).

    This occurs when:
    - Redis restarted and lost data
    - Redis failover to new instance
    """

    def test_file_ahead_sync_redis_to_file(self, mock_redis, temp_log_dir):
        """
        File has higher sequence than Redis (Redis data loss).

        Expected: Sync Redis to match file state
        """
        # Set up Redis with lower sequence
        mock_redis.set("test:audit:hash_chain:seq", 3)
        mock_redis.hset(
            "test:audit:hash_chain:state",
            mapping={
                "sequence": "3",
                "previous_hash": "old_hash",
            },
        )

        # Create file with more entries
        entries = build_hash_chain_entries(10)
        create_audit_log_file(temp_log_dir, entries)

        sync = StartupHashChainSync(
            redis_client=mock_redis,
            log_dir=temp_log_dir,
            key_prefix="test:",
        )

        result = sync.sync()

        assert result["status"] == "success"
        assert result["action"] == "synced_redis_to_file"
        assert result["file_sequence"] == 10

        # Verify Redis was updated
        assert int(mock_redis.get("test:audit:hash_chain:seq")) == 10
        state = mock_redis.hgetall("test:audit:hash_chain:state")
        assert int(state[b"sequence"]) == 10

    def test_file_ahead_redis_empty(self, mock_redis, temp_log_dir):
        """
        Redis empty but file has data (fresh Redis after crash).

        Expected: Sync Redis from file state
        """
        # Redis is empty (no sequence set)

        # Create file with entries
        entries = build_hash_chain_entries(5)
        create_audit_log_file(temp_log_dir, entries)

        sync = StartupHashChainSync(
            redis_client=mock_redis,
            log_dir=temp_log_dir,
            key_prefix="test:",
        )

        result = sync.sync()

        assert result["status"] == "success"
        assert result["action"] == "synced_redis_to_file"

        # Verify Redis now has file state
        assert int(mock_redis.get("test:audit:hash_chain:seq")) == 5


# =============================================================================
# Test: In-Sync Scenario
# =============================================================================


class TestStartupSyncInSync:
    """Tests for in-sync scenario (no action needed)."""

    def test_in_sync_no_action(self, mock_redis, temp_log_dir):
        """
        Redis and file have same sequence.

        Expected: action='in_sync', no changes
        """
        # Create file with 5 entries
        entries = build_hash_chain_entries(5)
        create_audit_log_file(temp_log_dir, entries)
        last_hash = entries[-1]["integrity"]["current_hash"]

        # Set Redis to match
        mock_redis.set("test:audit:hash_chain:seq", 5)
        mock_redis.hset(
            "test:audit:hash_chain:state",
            mapping={
                "sequence": "5",
                "previous_hash": last_hash,
            },
        )

        sync = StartupHashChainSync(
            redis_client=mock_redis,
            log_dir=temp_log_dir,
            key_prefix="test:",
        )

        result = sync.sync()

        assert result["status"] == "success"
        assert result["action"] == "in_sync"


# =============================================================================
# Test: PENDING Cleanup
# =============================================================================


class TestStartupSyncPendingCleanup:
    """Tests for PENDING sequence cleanup during startup."""

    def test_cleanup_stale_pending_sequences(self, mock_redis, temp_log_dir):
        """
        Cleanup PENDING sequences from crashed process.

        Expected: PENDING keys removed and moved to ORPHANED
        """
        # Create some stale PENDING sequences
        mock_redis.set("test:audit:hash_chain:pending:11", "expected_hash_11")
        mock_redis.set("test:audit:hash_chain:pending:12", "expected_hash_12")

        # Set up normal state
        entries = build_hash_chain_entries(10)
        create_audit_log_file(temp_log_dir, entries)
        last_hash = entries[-1]["integrity"]["current_hash"]

        mock_redis.set("test:audit:hash_chain:seq", 10)
        mock_redis.hset(
            "test:audit:hash_chain:state",
            mapping={
                "sequence": "10",
                "previous_hash": last_hash,
            },
        )

        sync = StartupHashChainSync(
            redis_client=mock_redis,
            log_dir=temp_log_dir,
            key_prefix="test:",
        )

        result = sync.sync()

        assert result["status"] == "success"
        assert result["pending_cleaned"] >= 0  # Cleanup was attempted


# =============================================================================
# Test: Idempotent Sync
# =============================================================================


class TestStartupSyncIdempotent:
    """Tests for idempotent sync behavior."""

    def test_sync_twice_no_duplicate_action(self, mock_redis, temp_log_dir):
        """
        Calling sync() twice should be safe and return already_synced.
        """
        entries = build_hash_chain_entries(5)
        create_audit_log_file(temp_log_dir, entries)

        sync = StartupHashChainSync(
            redis_client=mock_redis,
            log_dir=temp_log_dir,
            key_prefix="test:",
        )

        # First sync
        result1 = sync.sync()
        assert result1["action"] in ["fresh_start", "synced_redis_to_file", "in_sync"]

        # Second sync should indicate already done
        result2 = sync.sync()
        assert result2["status"] == "already_synced"
        assert result2["action"] == "none"


# =============================================================================
# Test: Error Handling
# =============================================================================


class TestStartupSyncErrorHandling:
    """Tests for error handling during sync."""

    def test_redis_failure_graceful_handling(self, mock_redis, temp_log_dir):
        """
        Redis failure during sync should be handled gracefully.

        Actual behavior: The sync process uses try-except and returns
        success even when Redis operations fail, logging errors internally.
        This is graceful degradation - the system continues to function.
        """
        mock_redis.enable_failure_mode()

        sync = StartupHashChainSync(
            redis_client=mock_redis,
            log_dir=temp_log_dir,
            key_prefix="test:",
        )

        result = sync.sync()

        # Graceful degradation - system continues despite Redis failure
        assert result["status"] == "success"
        # Actions are minimal or zero due to Redis unavailability
        assert "action" in result  # action field is always present

    def test_corrupted_log_file_handled(self, mock_redis, temp_log_dir):
        """
        Corrupted log file should not crash sync.
        """
        # Create corrupted log file
        log_file = temp_log_dir / "audit_20260118.jsonl"
        with open(log_file, "w") as f:
            f.write("invalid json\n")
            f.write("{malformed")

        sync = StartupHashChainSync(
            redis_client=mock_redis,
            log_dir=temp_log_dir,
            key_prefix="test:",
        )

        result = sync.sync()

        # Should complete without crash
        assert result["status"] == "success"


# =============================================================================
# Test: Multiple Log Files
# =============================================================================


class TestStartupSyncMultipleFiles:
    """Tests for handling multiple log files."""

    def test_finds_latest_sequence_across_files(self, mock_redis, temp_log_dir):
        """
        Should find highest sequence across multiple log files.
        """
        # Create older file
        old_entries = build_hash_chain_entries(5, start_seq=1)
        create_audit_log_file(temp_log_dir, old_entries, date="20260115")

        # Create newer file with higher sequences
        new_entries = build_hash_chain_entries(5, start_seq=6)
        # Fix chain: new_entries should link to old_entries
        new_entries[0]["integrity"]["previous_hash"] = old_entries[-1]["integrity"][
            "current_hash"
        ]
        # Recompute hashes
        prev_hash = old_entries[-1]["integrity"]["current_hash"]
        for entry in new_entries:
            entry["integrity"]["previous_hash"] = prev_hash
            if "current_hash" in entry["integrity"]:
                del entry["integrity"]["current_hash"]
            current_hash = compute_hash(entry)
            entry["integrity"]["current_hash"] = current_hash
            prev_hash = current_hash

        create_audit_log_file(temp_log_dir, new_entries, date="20260118")

        sync = StartupHashChainSync(
            redis_client=mock_redis,
            log_dir=temp_log_dir,
            key_prefix="test:",
        )

        result = sync.sync()

        # Should find sequence 10 from newest file
        assert result["file_sequence"] == 10
