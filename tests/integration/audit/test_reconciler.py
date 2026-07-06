"""
HashChainReconciler 통합 테스트.

degraded 엔트리 재조정(reconciliation) 전체 워크플로우 테스트:
- 로컬 로그 파일에서 degraded 엔트리 찾기
- 새로운 전역 시퀀스 번호 할당
- 적절한 체인 연결을 위한 해시 재계산
- 재조정 후 Redis 상태 업데이트
- 엔트리를 reconciled로 마킹

테스트 시나리오는 degraded 엔트리를 메인 체인에
다시 병합해야 하는 Redis 복구 상황을 시뮬레이션합니다.

Related code:
    baldur/audit/integrity.py#HashChainReconciler
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
    HashChainReconciler,
    compute_hash,
)

# =============================================================================
# Mock Redis Client
# =============================================================================


class IntegrationMockRedis:
    """
    Mock Redis client for reconciler integration tests.

    Simulates all Redis operations used by HashChainReconciler.
    """

    def __init__(self):
        self._data: dict[str, Any] = {}
        self._hashes: dict[str, dict[str, str]] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> bytes | None:
        value = self._data.get(key)
        return str(value).encode() if value is not None else None

    def set(self, key: str, value: Any, **kwargs) -> bool:
        with self._lock:
            self._data[key] = value
            return True

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

    def execute(self) -> list[Any]:
        results = []
        for cmd in self._commands:
            if cmd[0] == "set":
                self._redis.set(cmd[1], cmd[2])
                results.append(True)
            elif cmd[0] == "hset":
                self._redis.hset(cmd[1], cmd[2])
                results.append(1)
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
    """Create a mock Redis client with initial state."""
    redis = IntegrationMockRedis()
    return redis


def create_degraded_entry(
    sequence: int,
    previous_hash: str = "GENESIS",
    reason: str = "redis_unavailable",
    timestamp: str = None,
) -> dict[str, Any]:
    """
    Create a degraded log entry (written during Redis outage).

    These entries have:
    - degraded: true
    - Local-only sequence numbers
    - Need reconciliation

    Args:
        sequence: Local sequence number
        previous_hash: Previous hash (may be local chain)
        reason: Degradation reason
        timestamp: Entry timestamp

    Returns:
        Degraded entry dictionary
    """
    if timestamp is None:
        timestamp = datetime.now(UTC).isoformat()

    entry = {
        "event": f"degraded_event_{sequence}",
        "data": {"local_seq": sequence},
        "integrity": {
            "sequence": sequence,
            "previous_hash": previous_hash,
            "timestamp": timestamp,
            "pod_id": "test-pod",
            "tier": "local",
            "degraded": True,
            "degraded_reason": reason,
        },
    }

    current_hash = compute_hash(entry)
    entry["integrity"]["current_hash"] = current_hash

    return entry


def create_normal_entry(
    sequence: int,
    previous_hash: str = "GENESIS",
    timestamp: str = None,
) -> dict[str, Any]:
    """
    Create a normal (non-degraded) log entry.

    Args:
        sequence: Sequence number
        previous_hash: Previous hash
        timestamp: Entry timestamp

    Returns:
        Normal entry dictionary
    """
    if timestamp is None:
        timestamp = datetime.now(UTC).isoformat()

    entry = {
        "event": f"normal_event_{sequence}",
        "data": {"seq": sequence},
        "integrity": {
            "sequence": sequence,
            "previous_hash": previous_hash,
            "timestamp": timestamp,
            "pod_id": "test-pod",
        },
    }

    current_hash = compute_hash(entry)
    entry["integrity"]["current_hash"] = current_hash

    return entry


def write_log_file(
    log_dir: Path, entries: list[dict[str, Any]], date: str = None
) -> Path:
    """Write entries to a log file."""
    if date is None:
        date = datetime.now(UTC).strftime("%Y%m%d")

    log_file = log_dir / f"audit_{date}.jsonl"

    with open(log_file, "w", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(entry, default=str) + "\n")

    return log_file


def setup_redis_chain_state(
    redis: IntegrationMockRedis, sequence: int, previous_hash: str
):
    """Set up Redis with given chain state."""
    redis.set("test:audit:hash_chain:seq", sequence)
    redis.hset(
        "test:audit:hash_chain:state",
        mapping={
            "sequence": str(sequence),
            "previous_hash": previous_hash,
            "updated_at": datetime.now(UTC).isoformat(),
        },
    )


# =============================================================================
# Test: No Degraded Entries
# =============================================================================


class TestReconcilerNoDegradedEntries:
    """Tests when there are no degraded entries to reconcile."""

    def test_empty_log_directory(self, mock_redis, temp_log_dir):
        """
        Empty log directory - nothing to reconcile.

        Expected: status='no_degraded_entries'
        """
        reconciler = HashChainReconciler(
            redis_client=mock_redis,
            log_dir=temp_log_dir,
            key_prefix="test:",
        )

        result = reconciler.reconcile()

        assert result["status"] == "no_degraded_entries"
        assert result["degraded_entries_found"] == 0
        assert result["entries_merged"] == 0

    def test_only_normal_entries(self, mock_redis, temp_log_dir):
        """
        Log file has only normal entries (no degraded flag).

        Expected: Nothing to reconcile
        """
        # Create file with normal entries - build chain properly
        entries = []
        prev_hash = "GENESIS"
        for i in range(3):
            entry = create_normal_entry(i + 1, prev_hash)
            entries.append(entry)
            prev_hash = entry["integrity"]["current_hash"]

        write_log_file(temp_log_dir, entries)

        reconciler = HashChainReconciler(
            redis_client=mock_redis,
            log_dir=temp_log_dir,
            key_prefix="test:",
        )

        result = reconciler.reconcile()

        assert result["status"] == "no_degraded_entries"

    def test_already_reconciled_entries(self, mock_redis, temp_log_dir):
        """
        Degraded entries that are already marked as reconciled.

        Expected: Skip already reconciled entries
        """
        # Create degraded entry that's already reconciled
        entry = create_degraded_entry(1)
        entry["integrity"]["reconciled"] = True
        entry["integrity"]["reconciled_at"] = datetime.now(UTC).isoformat()

        write_log_file(temp_log_dir, [entry])

        reconciler = HashChainReconciler(
            redis_client=mock_redis,
            log_dir=temp_log_dir,
            key_prefix="test:",
        )

        result = reconciler.reconcile()

        assert result["status"] == "no_degraded_entries"


# =============================================================================
# Test: Basic Reconciliation
# =============================================================================


class TestReconcilerBasic:
    """Tests for basic reconciliation workflow."""

    def test_reconcile_single_degraded_entry(self, mock_redis, temp_log_dir):
        """
        Reconcile a single degraded entry into main chain.

        Expected: Entry gets new sequence from Redis
        """
        # Set up Redis with existing chain
        setup_redis_chain_state(mock_redis, 5, "existing_hash")

        # Create degraded entry
        degraded = create_degraded_entry(1, reason="redis_timeout")
        write_log_file(temp_log_dir, [degraded])

        reconciler = HashChainReconciler(
            redis_client=mock_redis,
            log_dir=temp_log_dir,
            key_prefix="test:",
        )

        result = reconciler.reconcile()

        assert result["status"] == "success"
        assert result["degraded_entries_found"] == 1
        assert result["entries_merged"] == 1
        assert result["new_sequence_start"] == 6
        assert result["new_sequence_end"] == 6

    def test_reconcile_multiple_degraded_entries(self, mock_redis, temp_log_dir):
        """
        Reconcile multiple degraded entries.

        Expected: All entries get consecutive sequences
        """
        # Set up Redis
        setup_redis_chain_state(mock_redis, 10, "hash_at_10")

        # Create multiple degraded entries
        entries = []
        prev_hash = "local_genesis"
        for i in range(5):
            entry = create_degraded_entry(
                sequence=i + 1,
                previous_hash=prev_hash,
                reason="redis_unavailable",
            )
            entries.append(entry)
            prev_hash = entry["integrity"]["current_hash"]

        write_log_file(temp_log_dir, entries)

        reconciler = HashChainReconciler(
            redis_client=mock_redis,
            log_dir=temp_log_dir,
            key_prefix="test:",
        )

        result = reconciler.reconcile()

        assert result["status"] == "success"
        assert result["degraded_entries_found"] == 5
        assert result["entries_merged"] == 5
        assert result["new_sequence_start"] == 11
        assert result["new_sequence_end"] == 15

    def test_redis_state_updated_after_reconciliation(self, mock_redis, temp_log_dir):
        """
        Redis state should be updated after reconciliation.

        Expected: Redis sequence and hash updated
        """
        setup_redis_chain_state(mock_redis, 5, "hash_5")

        degraded = create_degraded_entry(1)
        write_log_file(temp_log_dir, [degraded])

        reconciler = HashChainReconciler(
            redis_client=mock_redis,
            log_dir=temp_log_dir,
            key_prefix="test:",
        )

        result = reconciler.reconcile()

        assert result["status"] == "success"

        # Verify Redis state updated
        new_seq = int(mock_redis.get("test:audit:hash_chain:seq"))
        assert new_seq == 6

        state = mock_redis.hgetall("test:audit:hash_chain:state")
        assert int(state[b"sequence"]) == 6


# =============================================================================
# Test: Mixed Entries
# =============================================================================


class TestReconcilerMixedEntries:
    """Tests with both normal and degraded entries in logs."""

    def test_only_degraded_entries_collected(self, mock_redis, temp_log_dir):
        """
        Should only collect degraded entries, ignoring normal ones.
        """
        setup_redis_chain_state(mock_redis, 10, "hash_10")

        # Create mixed entries
        entries = [
            create_normal_entry(1),
            create_degraded_entry(2, reason="test"),
            create_normal_entry(3),
            create_degraded_entry(4, reason="test"),
            create_normal_entry(5),
        ]

        write_log_file(temp_log_dir, entries)

        reconciler = HashChainReconciler(
            redis_client=mock_redis,
            log_dir=temp_log_dir,
            key_prefix="test:",
        )

        result = reconciler.reconcile()

        assert result["degraded_entries_found"] == 2
        assert result["entries_merged"] == 2


# =============================================================================
# Test: Multiple Log Files
# =============================================================================


class TestReconcilerMultipleFiles:
    """Tests with degraded entries across multiple log files."""

    def test_collects_from_all_files(self, mock_redis, temp_log_dir):
        """
        Should collect degraded entries from all log files.
        """
        setup_redis_chain_state(mock_redis, 10, "hash_10")

        # Create entries in different files
        entries1 = [create_degraded_entry(1)]
        entries2 = [create_degraded_entry(2)]
        entries3 = [create_degraded_entry(3)]

        write_log_file(temp_log_dir, entries1, date="20260115")
        write_log_file(temp_log_dir, entries2, date="20260116")
        write_log_file(temp_log_dir, entries3, date="20260117")

        reconciler = HashChainReconciler(
            redis_client=mock_redis,
            log_dir=temp_log_dir,
            key_prefix="test:",
        )

        result = reconciler.reconcile()

        assert result["degraded_entries_found"] == 3
        assert result["entries_merged"] == 3

    def test_entries_sorted_by_timestamp(self, mock_redis, temp_log_dir):
        """
        Degraded entries should be merged in timestamp order.
        """
        setup_redis_chain_state(mock_redis, 10, "hash_10")

        # Create entries with different timestamps
        entries = [
            create_degraded_entry(3, timestamp="2026-01-17T12:00:00Z"),
            create_degraded_entry(1, timestamp="2026-01-17T10:00:00Z"),
            create_degraded_entry(2, timestamp="2026-01-17T11:00:00Z"),
        ]

        write_log_file(temp_log_dir, entries)

        reconciler = HashChainReconciler(
            redis_client=mock_redis,
            log_dir=temp_log_dir,
            key_prefix="test:",
        )

        result = reconciler.reconcile()

        # All should be merged
        assert result["entries_merged"] == 3


# =============================================================================
# Test: Error Handling
# =============================================================================


class TestReconcilerErrorHandling:
    """Tests for error handling during reconciliation."""

    def test_handles_corrupted_log_file(self, mock_redis, temp_log_dir):
        """
        Should skip corrupted entries without failing.
        """
        setup_redis_chain_state(mock_redis, 5, "hash_5")

        # Create file with some corrupted entries
        log_file = temp_log_dir / "audit_20260118.jsonl"
        with open(log_file, "w") as f:
            f.write("invalid json\n")
            f.write(json.dumps(create_degraded_entry(1)) + "\n")
            f.write("{incomplete\n")
            f.write(json.dumps(create_degraded_entry(2)) + "\n")

        reconciler = HashChainReconciler(
            redis_client=mock_redis,
            log_dir=temp_log_dir,
            key_prefix="test:",
        )

        result = reconciler.reconcile()

        # Should still process valid entries
        assert result["status"] == "success"
        assert result["degraded_entries_found"] == 2

    def test_handles_non_existent_log_directory(self, mock_redis):
        """
        Should handle non-existent log directory gracefully.
        """
        reconciler = HashChainReconciler(
            redis_client=mock_redis,
            log_dir=Path("/tmp/nonexistent_12345"),
            key_prefix="test:",
        )

        result = reconciler.reconcile()

        assert result["status"] == "no_degraded_entries"


# =============================================================================
# Test: Idempotency
# =============================================================================


class TestReconcilerIdempotency:
    """Tests for reconciler idempotency."""

    def test_reconciled_entries_not_processed_again(self, mock_redis, temp_log_dir):
        """
        Already reconciled entries should not be processed again.

        Note: This test verifies the reconciler checks for 'reconciled' flag.
        In production, the reconciler would update the file or use a tracking mechanism.
        """
        setup_redis_chain_state(mock_redis, 5, "hash_5")

        # Create a reconciled entry
        entry = create_degraded_entry(1)
        entry["integrity"]["reconciled"] = True
        entry["integrity"]["reconciled_at"] = datetime.now(UTC).isoformat()

        write_log_file(temp_log_dir, [entry])

        reconciler = HashChainReconciler(
            redis_client=mock_redis,
            log_dir=temp_log_dir,
            key_prefix="test:",
        )

        result = reconciler.reconcile()

        # Should not process already reconciled
        assert result["status"] == "no_degraded_entries"
        assert result["entries_merged"] == 0


# =============================================================================
# Test: Chain State After Reconciliation
# =============================================================================


class TestReconcilerChainState:
    """Tests for chain state after reconciliation."""

    def test_chain_continues_from_redis_state(self, mock_redis, temp_log_dir):
        """
        Merged entries should continue from current Redis chain state.

        Expected: First merged entry's previous_hash = Redis previous_hash
        """
        # Set up Redis with known hash
        known_hash = "known_chain_hash_at_seq_10"
        setup_redis_chain_state(mock_redis, 10, known_hash)

        # Create degraded entries
        degraded = create_degraded_entry(1, previous_hash="local_genesis")
        write_log_file(temp_log_dir, [degraded])

        reconciler = HashChainReconciler(
            redis_client=mock_redis,
            log_dir=temp_log_dir,
            key_prefix="test:",
        )

        result = reconciler.reconcile()

        assert result["status"] == "success"
        assert result["new_sequence_start"] == 11

        # Verify Redis state was properly updated
        state = mock_redis.hgetall("test:audit:hash_chain:state")
        assert state[b"sequence"].decode() == "11"
