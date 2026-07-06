"""
DLQ WATCH/MULTI Atomic Acquire Integration Tests

Verifies try_acquire_for_replay() works correctly against a real Redis
instance, including atomicity under concurrency. The acquire uses a
WATCH/MULTI optimistic-lock transaction (originally a Lua script in 446;
migrated to WATCH/MULTI when the entry blob moved to STRING+zlib in 502),
so the concurrency guard is the watched key plus the queued MULTI exec.

Test Categories:
    A. Acquire Round-Trip:
        - Acquire pending entry transitions to replaying
        - Nonexistent entry returns None
        - Non-pending entry returns None (status mismatch)
        - Max retries exceeded returns None
        - Entry removed from pending sorted set after acquire
        - Entry indexed in the replaying status set after acquire
        - Original entry data preserved after acquire
    B. Concurrent Acquire Atomicity:
        - Only one thread wins when 20 threads compete
        - Retry count is exactly 1 after concurrent acquire
    C. Full Lifecycle:
        - Create → acquire → complete cycle through real Redis

Note: All tests require a running Redis instance.
      Marked with @pytest.mark.requires_redis for auto-skip.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import pytest

pytestmark = pytest.mark.requires_redis


@pytest.fixture(autouse=True)
def _reset_redis_unavailable_flag():
    """Reset runtime-scoped Redis negative cache so backend can init Redis."""
    from baldur.adapters.redis import _redis_state

    state = _redis_state()
    state.unavailable = False
    state.fail_time = 0.0
    yield
    state.unavailable = False
    state.fail_time = 0.0


# =============================================================================
# A. Acquire Round-Trip Tests
# =============================================================================


class TestWatchMultiAcquireRoundTrip:
    """Verifies WATCH/MULTI acquire works against real Redis."""

    def test_acquire_pending_entry_transitions_to_replaying(
        self, redis_dlq_repository, redis_client
    ):
        """
        Purpose:
            Create a pending entry, acquire it, and verify the status
            transitions to 'replaying'.
        Expected:
            - Acquire returns a FailedOperationData
            - Returned entry has status='replaying'
            - Retry count is incremented to 1
        """
        entry = redis_dlq_repository.create(
            domain="payment",
            failure_type="PG_TIMEOUT",
            error_message="Connection timeout",
        )

        result = redis_dlq_repository.try_acquire_for_replay(id=entry.id, max_retries=3)

        assert result is not None
        assert result.status == "replaying"
        assert result.retry_count == 1
        assert result.domain == "payment"

    def test_acquire_nonexistent_entry_returns_none(self, redis_dlq_repository):
        """
        Purpose:
            Attempt to acquire an entry ID that does not exist.
        Expected:
            - Returns None (no crash, no exception)
        """
        result = redis_dlq_repository.try_acquire_for_replay(id=999999, max_retries=3)

        assert result is None

    def test_acquire_non_pending_entry_returns_none(self, redis_dlq_repository):
        """
        Purpose:
            Acquire an entry that is already in 'replaying' status.
        Expected:
            - Returns None (status precondition not met)
        """
        entry = redis_dlq_repository.create(
            domain="inventory",
            failure_type="DB_ERROR",
        )

        # First acquire succeeds
        first = redis_dlq_repository.try_acquire_for_replay(id=entry.id, max_retries=3)
        assert first is not None

        # Second acquire fails (status is now 'replaying')
        second = redis_dlq_repository.try_acquire_for_replay(id=entry.id, max_retries=3)
        assert second is None

    def test_acquire_max_retries_exceeded_returns_none(self, redis_dlq_repository):
        """
        Purpose:
            Acquire an entry whose retry_count already >= max_retries.
        Expected:
            - Returns None (retry budget exhausted)
        """
        entry = redis_dlq_repository.create(
            domain="webhook",
            failure_type="HTTP_500",
            retry_count=3,
            max_retries=3,
        )

        result = redis_dlq_repository.try_acquire_for_replay(id=entry.id, max_retries=3)

        assert result is None

    def test_acquire_removes_entry_from_pending_set(self, redis_dlq_repository):
        """
        Purpose:
            After acquire, the entry should no longer be in the
            pending sorted set.
        Expected:
            - count_pending decreases by 1 after acquire
            - Entry does not appear in get_pending() results
        """
        entry = redis_dlq_repository.create(
            domain="payment",
            failure_type="PG_TIMEOUT",
        )
        pending_before = redis_dlq_repository.count_pending()
        assert pending_before >= 1

        redis_dlq_repository.try_acquire_for_replay(id=entry.id, max_retries=3)

        pending_after = redis_dlq_repository.count_pending()
        assert pending_after == pending_before - 1

        pending_entries = redis_dlq_repository.get_pending()
        pending_ids = [e.id for e in pending_entries]
        assert entry.id not in pending_ids

    def test_acquire_indexes_entry_in_replaying_status_set(self, redis_dlq_repository):
        """
        Purpose:
            The atomic acquire block (which bypasses _update's index
            maintenance) must itself zadd the entry into the REPLAYING status
            index, so find(status='replaying') / count(status='replaying')
            serve the acquired entry against real Redis. This is the
            normal-mode atomic-MULTI path the degraded integration test cannot
            reach.
        Expected:
            - find(status='replaying') includes the acquired entry id
            - count(status='replaying') reflects it
        """
        entry = redis_dlq_repository.create(
            domain="payment",
            failure_type="PG_TIMEOUT",
        )

        acquired = redis_dlq_repository.try_acquire_for_replay(
            id=entry.id, max_retries=3
        )
        assert acquired is not None
        assert acquired.status == "replaying"

        replaying = redis_dlq_repository.find(status="replaying")
        assert entry.id in {e.id for e in replaying}
        assert redis_dlq_repository.count(status="replaying") >= 1

    def test_acquire_preserves_original_entry_data(self, redis_dlq_repository):
        """
        Purpose:
            Verify that fields not modified by the acquire are preserved.
        Expected:
            - domain, failure_type, error_message, max_retries unchanged
            - created_at preserved
        """
        entry = redis_dlq_repository.create(
            domain="point",
            failure_type="BALANCE_ERROR",
            error_message="Insufficient balance",
            max_retries=5,
        )

        result = redis_dlq_repository.try_acquire_for_replay(id=entry.id, max_retries=5)

        assert result is not None
        assert result.domain == "point"
        assert result.failure_type == "BALANCE_ERROR"
        assert result.error_message == "Insufficient balance"
        assert result.max_retries == 5
        assert result.created_at is not None

    def test_acquire_sets_last_retry_at(self, redis_dlq_repository):
        """
        Purpose:
            Verify that the acquire sets the last_retry_at timestamp.
        Expected:
            - last_retry_at is set (not None) after acquire
        """
        entry = redis_dlq_repository.create(
            domain="payment",
            failure_type="PG_TIMEOUT",
        )

        result = redis_dlq_repository.try_acquire_for_replay(id=entry.id, max_retries=3)

        assert result is not None
        assert result.last_retry_at is not None


# =============================================================================
# B. Concurrent Acquire Atomicity Tests
# =============================================================================


class TestConcurrentAcquireAtomicity:
    """Verifies concurrent acquire attempts are serialized by WATCH/MULTI."""

    def test_only_one_thread_acquires_when_twenty_compete(self, redis_dlq_repository):
        """
        Purpose:
            Spawn 20 threads that simultaneously try to acquire the
            same DLQ entry. The WATCH/MULTI optimistic lock must let
            exactly one succeed; the rest see a WatchError-driven retry
            that re-reads the now-replaying status and returns None.
        Expected:
            - Exactly 1 thread returns a non-None result
            - 19 threads return None
            - Entry status is 'replaying' with retry_count=1
        """
        entry = redis_dlq_repository.create(
            domain="payment",
            failure_type="PG_TIMEOUT",
        )

        results = []
        barrier = threading.Barrier(20)

        def try_acquire():
            barrier.wait()
            return redis_dlq_repository.try_acquire_for_replay(
                id=entry.id, max_retries=10
            )

        with ThreadPoolExecutor(max_workers=20) as executor:
            futures = [executor.submit(try_acquire) for _ in range(20)]
            for future in as_completed(futures):
                results.append(future.result())

        successes = [r for r in results if r is not None]
        assert len(successes) == 1

        # Verify final state in Redis
        final = redis_dlq_repository.get_by_id(entry.id)
        assert final is not None
        assert final.status == "replaying"
        assert final.retry_count == 1

    def test_concurrent_acquire_different_entries_all_succeed(
        self, redis_dlq_repository
    ):
        """
        Purpose:
            Create 10 entries, each acquired by a different thread.
            All should succeed since there's no contention.
        Expected:
            - All 10 acquire attempts succeed
            - Each entry transitions to 'replaying'
        """
        entries = [
            redis_dlq_repository.create(
                domain="payment",
                failure_type="PG_TIMEOUT",
            )
            for _ in range(10)
        ]

        results = []

        def try_acquire(eid):
            return redis_dlq_repository.try_acquire_for_replay(id=eid, max_retries=5)

        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = {executor.submit(try_acquire, e.id): e.id for e in entries}
            for future in as_completed(futures):
                results.append(future.result())

        successes = [r for r in results if r is not None]
        assert len(successes) == 10


# =============================================================================
# C. Full Lifecycle Tests
# =============================================================================


class TestFullLifecycleWithWatchMulti:
    """Verifies complete create → acquire → complete cycle via real Redis."""

    def test_create_acquire_resolve_lifecycle(self, redis_dlq_repository):
        """
        Purpose:
            Full lifecycle: create pending → acquire → complete
            as resolved. Verify state at each step.
        Expected:
            - After create: status=pending, in pending set
            - After acquire: status=replaying, not in pending set
            - After complete: status=resolved
        """
        # Phase 1: Create
        entry = redis_dlq_repository.create(
            domain="payment",
            failure_type="PG_TIMEOUT",
        )
        assert entry.status == "pending"
        assert redis_dlq_repository.count_pending() >= 1

        # Phase 2: Acquire
        acquired = redis_dlq_repository.try_acquire_for_replay(
            id=entry.id, max_retries=3
        )
        assert acquired is not None
        assert acquired.status == "replaying"

        # Phase 3: Complete
        success = redis_dlq_repository.complete_replay(
            id=entry.id,
            success=True,
            resolution_type="auto_replay",
        )
        assert success is True

        final = redis_dlq_repository.get_by_id(entry.id)
        assert final is not None
        assert final.status == "resolved"

    def test_create_acquire_fail_returns_to_pending(self, redis_dlq_repository):
        """
        Purpose:
            Lifecycle where replay fails: create → acquire → fail.
            Entry should return to pending for retry.
        Expected:
            - After failed complete: status=pending (if retries remain)
            - Entry reappears in pending set
        """
        entry = redis_dlq_repository.create(
            domain="inventory",
            failure_type="DB_ERROR",
            max_retries=3,
        )

        acquired = redis_dlq_repository.try_acquire_for_replay(
            id=entry.id, max_retries=3
        )
        assert acquired is not None

        redis_dlq_repository.complete_replay(
            id=entry.id,
            success=False,
            note="Handler returned error",
        )

        final = redis_dlq_repository.get_by_id(entry.id)
        assert final is not None
        assert final.status == "pending"

    def test_acquire_twice_after_fail_increments_retry_count(
        self, redis_dlq_repository
    ):
        """
        Purpose:
            Acquire → fail → re-acquire cycle. Verify retry_count
            increments correctly across multiple acquires.
        Expected:
            - First acquire: retry_count=1
            - After fail + re-acquire: retry_count=2
        """
        entry = redis_dlq_repository.create(
            domain="payment",
            failure_type="PG_TIMEOUT",
            max_retries=5,
        )

        # First acquire
        first = redis_dlq_repository.try_acquire_for_replay(id=entry.id, max_retries=5)
        assert first is not None
        assert first.retry_count == 1

        # Fail and return to pending
        redis_dlq_repository.complete_replay(id=entry.id, success=False, note="Retry")

        # Second acquire
        second = redis_dlq_repository.try_acquire_for_replay(id=entry.id, max_retries=5)
        assert second is not None
        assert second.retry_count == 2
