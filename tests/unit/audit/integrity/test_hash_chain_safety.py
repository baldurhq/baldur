"""
Tests for Hash Chain Safety Components.

Covers zero-data-loss and integrity guarantee features:
- MonotonicTimer: Clock-independent TTL using time.monotonic()
- MonotonicTimestamp: Always-increasing timestamp generation
- HashChainWAL: Write-ahead log for crash recovery
- AtomicMergeSwap: Global lock preventing concurrent reconciliation
- ShardedDateLock: Per-date parallel lock
- IntegrityAuditTrail: Forensic event recording

Refactored to use Factory Pattern (Phase 4):
- MockRedisClient → factories.MockRedisClient
"""

import json
import threading
import time
from datetime import UTC, datetime

import pytest

from baldur.audit.hash_chain_safety import (
    AtomicMergeSwap,
    HashChainSafetyManager,
    HashChainWAL,
    IntegrityAuditTrail,
    IntegrityEventType,
    MonotonicTimer,
    MonotonicTimestamp,
    ShardedDateLock,
)

# Factory Pattern imports
from tests.factories import MockRedisClient

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def mock_redis():
    return MockRedisClient()


@pytest.fixture
def failing_redis():
    return MockRedisClient(should_fail=True)


@pytest.fixture
def temp_log_dir(tmp_path):
    log_dir = tmp_path / "audit"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir


# =============================================================================
# MonotonicTimer Tests
# =============================================================================


class TestMonotonicTimer:
    """Tests for MonotonicTimer."""

    def test_timer_not_started(self):
        """Timer should not be expired if not started."""
        timer = MonotonicTimer(ttl_seconds=1.0)

        assert not timer.is_started()
        assert not timer.is_expired()
        assert timer.elapsed_seconds() == 0.0

    def test_timer_start_and_expire(self):
        """Timer should expire after TTL."""
        timer = MonotonicTimer(ttl_seconds=0.1)
        timer.start()

        assert timer.is_started()
        assert not timer.is_expired()

        time.sleep(0.15)

        assert timer.is_expired()

    def test_elapsed_seconds(self):
        """Elapsed seconds should increase monotonically."""
        timer = MonotonicTimer(ttl_seconds=10.0)
        timer.start()

        time.sleep(0.05)
        elapsed1 = timer.elapsed_seconds()

        time.sleep(0.05)
        elapsed2 = timer.elapsed_seconds()

        assert elapsed2 > elapsed1
        assert elapsed1 >= 0.04  # Allow some tolerance
        assert elapsed2 >= 0.09

    def test_remaining_seconds(self):
        """Remaining seconds should decrease."""
        timer = MonotonicTimer(ttl_seconds=1.0)
        timer.start()

        remaining1 = timer.remaining_seconds()
        time.sleep(0.1)
        remaining2 = timer.remaining_seconds()

        assert remaining2 < remaining1
        assert remaining2 >= 0.0

    def test_reset(self):
        """Reset should restart the timer."""
        timer = MonotonicTimer(ttl_seconds=0.1)
        timer.start()

        time.sleep(0.15)
        assert timer.is_expired()

        timer.reset()
        assert not timer.is_expired()

    def test_fluent_interface(self):
        """Start and reset should return self for chaining."""
        timer = MonotonicTimer(ttl_seconds=1.0)

        result = timer.start()
        assert result is timer

        result = timer.reset()
        assert result is timer


# =============================================================================
# MonotonicTimestamp Tests
# =============================================================================


class TestMonotonicTimestamp:
    """Tests for MonotonicTimestamp."""

    def test_generates_timestamp(self):
        """Should generate valid ISO timestamp."""
        ts = MonotonicTimestamp()
        timestamp = ts.now()

        # Should be valid ISO format
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        assert parsed is not None

    def test_monotonically_increasing(self):
        """Timestamps should always increase."""
        ts = MonotonicTimestamp()

        timestamps = [ts.now() for _ in range(100)]

        for i in range(1, len(timestamps)):
            assert timestamps[i] >= timestamps[i - 1]

    def test_clock_skew_protection(self):
        """Should handle clock going backward."""
        ts = MonotonicTimestamp()

        # Get first timestamp
        first = ts.now()

        # Simulate clock going backward by manipulating internal state
        ts._last_timestamp = datetime.now(UTC)  # Force a future timestamp

        # Next call should still be >= last
        second = ts.now()
        assert second >= first

    def test_get_stats(self):
        """Stats should track offset."""
        ts = MonotonicTimestamp()
        ts.now()

        stats = ts.get_stats()
        assert "total_offset_seconds" in stats
        assert "last_timestamp" in stats


# =============================================================================
# HashChainWAL Tests
# =============================================================================


class TestHashChainWAL:
    """Tests for HashChainWAL."""

    def test_write_pending(self, temp_log_dir):
        """Should write PENDING entry to WAL."""
        wal = HashChainWAL(wal_dir=temp_log_dir / "wal")

        seq = wal.write_pending(
            operation="WRITE",
            entry_data={"event": "test"},
            expected_hash="abc123",
        )

        assert seq == 1
        wal.close()

        # Verify file was written
        wal_file = temp_log_dir / "wal" / "hash_chain_wal.jsonl"
        assert wal_file.exists()

        content = wal_file.read_text()
        data = json.loads(content.strip())
        assert data["seq"] == 1
        assert data["op"] == "WRITE"
        assert data["status"] == "PENDING"

    def test_sequence_increments(self, temp_log_dir):
        """Sequence should increment with each write."""
        wal = HashChainWAL(wal_dir=temp_log_dir / "wal")

        seq1 = wal.write_pending("WRITE", {}, "hash1")
        seq2 = wal.write_pending("WRITE", {}, "hash2")
        seq3 = wal.write_pending("ANCHOR", {}, "hash3")

        assert seq1 == 1
        assert seq2 == 2
        assert seq3 == 3

        wal.close()

    def test_mark_committed(self, temp_log_dir):
        """Should append COMMITTED marker."""
        wal = HashChainWAL(wal_dir=temp_log_dir / "wal")

        seq = wal.write_pending("WRITE", {"key": "value"}, "hash123")
        result = wal.mark_committed(seq)

        assert result is True
        wal.close()

        # Verify committed marker exists
        wal_file = temp_log_dir / "wal" / "hash_chain_wal.jsonl"
        lines = wal_file.read_text().strip().split("\n")

        assert len(lines) == 2
        commit_marker = json.loads(lines[1])
        assert commit_marker["status"] == "COMMITTED"
        assert commit_marker["seq"] == seq

    def test_mark_aborted(self, temp_log_dir):
        """Should append ABORTED marker with reason."""
        wal = HashChainWAL(wal_dir=temp_log_dir / "wal")

        seq = wal.write_pending("WRITE", {}, "hash")
        wal.mark_aborted(seq, "test_failure")
        wal.close()

        wal_file = temp_log_dir / "wal" / "hash_chain_wal.jsonl"
        lines = wal_file.read_text().strip().split("\n")

        abort_marker = json.loads(lines[1])
        assert abort_marker["status"] == "ABORTED"
        assert abort_marker["reason"] == "test_failure"

    def test_get_uncommitted_entries(self, temp_log_dir):
        """Should return only uncommitted entries."""
        wal = HashChainWAL(wal_dir=temp_log_dir / "wal")

        # Write 3 entries
        seq1 = wal.write_pending("WRITE", {"id": 1}, "h1")
        seq2 = wal.write_pending("WRITE", {"id": 2}, "h2")
        seq3 = wal.write_pending("WRITE", {"id": 3}, "h3")

        # Commit 1, abort 2, leave 3 pending
        wal.mark_committed(seq1)
        wal.mark_aborted(seq2, "test")

        wal.close()

        # Reopen and get uncommitted
        wal2 = HashChainWAL(wal_dir=temp_log_dir / "wal")
        uncommitted = wal2.get_uncommitted_entries()

        assert len(uncommitted) == 1
        assert uncommitted[0].sequence == seq3
        assert uncommitted[0].entry_data == {"id": 3}

        wal2.close()

    def test_sequence_persists_across_restarts(self, temp_log_dir):
        """Sequence should continue after restart."""
        wal1 = HashChainWAL(wal_dir=temp_log_dir / "wal")
        wal1.write_pending("WRITE", {}, "h1")
        wal1.write_pending("WRITE", {}, "h2")
        wal1.close()

        wal2 = HashChainWAL(wal_dir=temp_log_dir / "wal")
        seq3 = wal2.write_pending("WRITE", {}, "h3")

        assert seq3 == 3
        wal2.close()

    def test_compact(self, temp_log_dir):
        """Compact should remove old entries."""
        wal = HashChainWAL(wal_dir=temp_log_dir / "wal")

        # Write and commit several entries
        for i in range(5):
            seq = wal.write_pending("WRITE", {"id": i}, f"h{i}")
            wal.mark_committed(seq)

        wal.close()

        # Compact keeping only seq > 3
        wal2 = HashChainWAL(wal_dir=temp_log_dir / "wal")
        removed = wal2.compact(keep_sequences_after=3)

        assert removed > 0
        wal2.close()


# =============================================================================
# AtomicMergeSwap Tests
# =============================================================================


class TestAtomicMergeSwap:
    """Tests for AtomicMergeSwap."""

    def test_acquire_and_release(self, mock_redis):
        """Should acquire and release lock."""
        with AtomicMergeSwap(mock_redis, timeout_seconds=10) as swap:
            assert swap.acquired is True

            # Lock should be in Redis
            lock_key = swap._get_lock_key()
            assert mock_redis.get(lock_key) is not None

        # Lock should be released
        assert mock_redis.get(lock_key) is None

    def test_mutual_exclusion(self, mock_redis):
        """Second acquire should fail while first holds lock."""
        with AtomicMergeSwap(mock_redis, blocking_timeout=0.2) as swap1:
            assert swap1.acquired is True

            # Second swap should fail (short timeout)
            with AtomicMergeSwap(mock_redis, blocking_timeout=0.1) as swap2:
                assert swap2.acquired is False

    def test_lock_release_after_exception(self, mock_redis):
        """Lock should be released even if exception occurs."""
        lock_key = None

        try:
            with AtomicMergeSwap(mock_redis, timeout_seconds=10) as swap:
                lock_key = swap._get_lock_key()
                assert swap.acquired is True
                raise ValueError("Test exception")
        except ValueError:
            pass

        # Lock should be released
        assert mock_redis.get(lock_key) is None

    def test_redis_failure(self, failing_redis):
        """Should handle Redis failure gracefully."""
        with AtomicMergeSwap(failing_redis, blocking_timeout=0.1) as swap:
            assert swap.acquired is False


# =============================================================================
# ShardedDateLock Tests
# =============================================================================


class TestShardedDateLock:
    """Tests for ShardedDateLock."""

    def test_date_specific_lock(self, mock_redis):
        """Should create date-specific lock."""
        with ShardedDateLock(mock_redis, "2024-01-15") as lock:
            assert lock.acquired is True

            lock_key = lock._get_lock_key()
            assert "2024-01-15" in lock_key
            assert mock_redis.get(lock_key) is not None

    def test_different_dates_parallel(self, mock_redis):
        """Different dates should not block each other."""
        with ShardedDateLock(mock_redis, "2024-01-15") as lock1:
            assert lock1.acquired is True

            with ShardedDateLock(mock_redis, "2024-01-16") as lock2:
                assert lock2.acquired is True  # Should succeed

    def test_same_date_mutual_exclusion(self, mock_redis):
        """Same date should be mutually exclusive."""
        with ShardedDateLock(mock_redis, "2024-01-15", blocking_timeout=0.1) as lock1:
            assert lock1.acquired is True

            with ShardedDateLock(
                mock_redis, "2024-01-15", blocking_timeout=0.1
            ) as lock2:
                assert lock2.acquired is False


# =============================================================================
# IntegrityAuditTrail Tests
# =============================================================================


class TestIntegrityAuditTrail:
    """Tests for IntegrityAuditTrail."""

    def test_record_to_redis(self, mock_redis):
        """Should record event to Redis."""
        trail = IntegrityAuditTrail(redis_client=mock_redis)

        event = trail.record(
            event_type=IntegrityEventType.CHAIN_VERIFIED,
            message="Chain verified successfully",
            details={"entries": 100},
        )

        assert event["type"] == IntegrityEventType.CHAIN_VERIFIED
        assert event["message"] == "Chain verified successfully"
        assert "timestamp" in event

        # Should be in Redis
        events = trail.get_recent_events()
        assert len(events) == 1
        assert events[0]["type"] == IntegrityEventType.CHAIN_VERIFIED

    def test_record_to_file(self, temp_log_dir):
        """Should record event to file."""
        trail = IntegrityAuditTrail(log_dir=temp_log_dir / "integrity")

        trail.record(
            event_type=IntegrityEventType.RECONCILIATION_STARTED,
            message="Starting reconciliation",
            severity="INFO",
        )

        # Verify file was written
        log_file = temp_log_dir / "integrity" / "integrity_trail.jsonl"
        assert log_file.exists()

        content = log_file.read_text()
        event = json.loads(content.strip())
        assert event["type"] == IntegrityEventType.RECONCILIATION_STARTED

    def test_record_both_redis_and_file(self, mock_redis, temp_log_dir):
        """Should record to both Redis and file."""
        trail = IntegrityAuditTrail(
            redis_client=mock_redis,
            log_dir=temp_log_dir / "integrity",
        )

        trail.record(
            event_type=IntegrityEventType.ANCHOR_CREATED,
            message="Daily anchor created",
            details={"date": "2024-01-15"},
        )

        # Verify Redis
        events = trail.get_recent_events()
        assert len(events) == 1

        # Verify file
        log_file = temp_log_dir / "integrity" / "integrity_trail.jsonl"
        assert log_file.exists()

    def test_get_recent_events(self, mock_redis):
        """Should return recent events in order."""
        trail = IntegrityAuditTrail(redis_client=mock_redis)

        for i in range(5):
            trail.record(
                event_type=IntegrityEventType.CHAIN_VERIFIED,
                message=f"Event {i}",
            )

        events = trail.get_recent_events(count=3)
        assert len(events) == 3
        # Most recent first (LIFO from lpush)
        assert events[0]["message"] == "Event 4"

    def test_get_events_by_type(self, mock_redis):
        """Should filter events by type."""
        trail = IntegrityAuditTrail(redis_client=mock_redis)

        trail.record(IntegrityEventType.CHAIN_VERIFIED, "Verified 1")
        trail.record(IntegrityEventType.CHAIN_BROKEN, "Broken!")
        trail.record(IntegrityEventType.CHAIN_VERIFIED, "Verified 2")

        verified_events = trail.get_events_by_type(IntegrityEventType.CHAIN_VERIFIED)
        assert len(verified_events) == 2
        assert all(
            e["type"] == IntegrityEventType.CHAIN_VERIFIED for e in verified_events
        )

    def test_max_redis_entries_trimmed(self, mock_redis):
        """Should trim Redis list to max size."""
        # 명시적으로 max_redis_entries를 작게 설정한 인스턴스 생성
        trail = IntegrityAuditTrail(redis_client=mock_redis, max_redis_entries=5)

        for i in range(10):
            trail.record(IntegrityEventType.CHAIN_VERIFIED, f"Event {i}")

        events = trail.get_recent_events(count=100)
        assert len(events) <= 5


# =============================================================================
# HashChainSafetyManager Tests
# =============================================================================


class TestHashChainSafetyManager:
    """Tests for HashChainSafetyManager."""

    def test_initialization(self, mock_redis, temp_log_dir):
        """Should initialize all components."""
        manager = HashChainSafetyManager(
            redis_client=mock_redis,
            log_dir=temp_log_dir,
        )

        assert manager.timestamp is not None
        assert manager.wal is not None
        assert manager.audit_trail is not None

        manager.close()

    def test_get_atomic_swap(self, mock_redis, temp_log_dir):
        """Should provide atomic swap context manager."""
        manager = HashChainSafetyManager(
            redis_client=mock_redis,
            log_dir=temp_log_dir,
        )

        swap = manager.get_atomic_swap()
        assert isinstance(swap, AtomicMergeSwap)

        manager.close()

    def test_get_date_lock(self, mock_redis, temp_log_dir):
        """Should provide date lock context manager."""
        manager = HashChainSafetyManager(
            redis_client=mock_redis,
            log_dir=temp_log_dir,
        )

        lock = manager.get_date_lock("2024-01-15")
        assert isinstance(lock, ShardedDateLock)

        manager.close()

    def test_no_redis_atomic_swap_fails(self, temp_log_dir):
        """Atomic swap without Redis should raise error."""
        manager = HashChainSafetyManager(log_dir=temp_log_dir)

        with pytest.raises(ValueError, match="Redis client required"):
            manager.get_atomic_swap()

        manager.close()


# =============================================================================
# Integration Tests
# =============================================================================


class TestHashChainSafetyIntegration:
    """Integration tests for hash chain safety components."""

    def test_full_wal_lifecycle(self, mock_redis, temp_log_dir):
        """Test complete WAL lifecycle with recovery."""
        manager = HashChainSafetyManager(
            redis_client=mock_redis,
            log_dir=temp_log_dir,
        )

        # Write entries
        seq1 = manager.wal.write_pending("WRITE", {"id": 1}, "hash1")
        seq2 = manager.wal.write_pending("WRITE", {"id": 2}, "hash2")

        # Commit first, leave second pending
        manager.wal.mark_committed(seq1)

        # Record events
        manager.audit_trail.record(
            IntegrityEventType.WAL_RECOVERY,
            "Recovery started",
            {"uncommitted_count": 1},
        )

        # Close and reopen (simulate restart)
        manager.close()

        manager2 = HashChainSafetyManager(
            redis_client=mock_redis,
            log_dir=temp_log_dir,
        )

        # Should find uncommitted entry
        uncommitted = manager2.wal.get_uncommitted_entries()
        assert len(uncommitted) == 1
        assert uncommitted[0].sequence == seq2

        manager2.close()

    def test_concurrent_date_locks(self, mock_redis, temp_log_dir):
        """Test parallel acquisition of different date locks."""
        manager = HashChainSafetyManager(
            redis_client=mock_redis,
            log_dir=temp_log_dir,
        )

        acquired_dates = []

        def try_lock(date: str):
            lock = manager.get_date_lock(date)
            with lock:
                if lock.acquired:
                    acquired_dates.append(date)
                    time.sleep(0.1)

        threads = [
            threading.Thread(target=try_lock, args=("2024-01-15",)),
            threading.Thread(target=try_lock, args=("2024-01-16",)),
            threading.Thread(target=try_lock, args=("2024-01-17",)),
        ]

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # All dates should be acquired (no conflicts)
        assert len(acquired_dates) == 3

        manager.close()
