"""
SemaphoreBulkhead try_acquire() timeout parameter unit tests.

Test items:
- Behavior: timeout=None tries immediately, non-blocking
- Behavior: a given timeout waits before acquiring
- Behavior: returns False when the timeout expires
- Behavior: every failure path records rejection stats (D6)
"""

import threading
import time

from baldur.services.bulkhead.semaphore import SemaphoreBulkhead


class TestTryAcquireTimeoutBehavior:
    """try_acquire() timeout parameter behavior verification."""

    def test_none_timeout_succeeds_immediately(self):
        """timeout=None returns True immediately when a slot is free."""
        bulkhead = SemaphoreBulkhead("test", max_concurrent=1)
        assert bulkhead.try_acquire(timeout=None) is True
        bulkhead.release()

    def test_none_timeout_fails_immediately_when_full(self):
        """timeout=None returns False immediately, without waiting, when full."""
        bulkhead = SemaphoreBulkhead("test", max_concurrent=1)
        bulkhead.try_acquire()  # occupy the slot

        start = time.monotonic()
        result = bulkhead.try_acquire(timeout=None)
        elapsed = time.monotonic() - start

        assert result is False
        assert elapsed < 0.05  # returned immediately
        bulkhead.release()

    def test_timeout_waits_and_succeeds(self):
        """A given timeout waits and acquires once a slot is released."""
        bulkhead = SemaphoreBulkhead("test", max_concurrent=1)
        bulkhead.try_acquire()  # occupy the slot

        def release_later():
            time.sleep(0.02)
            bulkhead.release()

        t = threading.Thread(target=release_later)
        t.start()

        result = bulkhead.try_acquire(timeout=0.5)
        assert result is True
        bulkhead.release()
        t.join()

    def test_timeout_expires_returns_false(self):
        """Returns False when the slot is not released within the timeout."""
        bulkhead = SemaphoreBulkhead("test", max_concurrent=1)
        bulkhead.try_acquire()  # occupy the slot

        start = time.monotonic()
        result = bulkhead.try_acquire(timeout=0.05)
        elapsed = time.monotonic() - start

        assert result is False
        assert elapsed >= 0.04  # waited for the timeout
        bulkhead.release()

    def test_none_timeout_failure_records_stats(self):
        """A non-blocking failure (timeout=None) records rejection stats so
        gate/admission sheds are visible in state (D6)."""
        # Given — a saturated capacity-1 bulkhead
        bulkhead = SemaphoreBulkhead("test", max_concurrent=1)
        bulkhead.try_acquire()  # occupy the slot

        # When — an immediate attempt is rejected
        assert bulkhead.try_acquire(timeout=None) is False

        # Then — the rejection is recorded
        state = bulkhead.get_state()
        assert state.rejected_count == 1
        assert state.last_rejection_time is not None
        bulkhead.release()

    def test_timed_failure_records_stats(self):
        """A timed-wait expiry records rejection stats (D6)."""
        # Given — a saturated capacity-1 bulkhead with no release pending
        bulkhead = SemaphoreBulkhead("test", max_concurrent=1)
        bulkhead.try_acquire()  # occupy the slot

        # When — a timed attempt expires
        assert bulkhead.try_acquire(timeout=0.05) is False

        # Then — the rejection is recorded
        state = bulkhead.get_state()
        assert state.rejected_count == 1
        assert state.last_rejection_time is not None
        bulkhead.release()

    def test_successful_acquire_does_not_record_rejection(self):
        """A successful try_acquire leaves rejection stats untouched."""
        bulkhead = SemaphoreBulkhead("test", max_concurrent=1)

        assert bulkhead.try_acquire() is True

        state = bulkhead.get_state()
        assert state.rejected_count == 0
        assert state.last_rejection_time is None
        bulkhead.release()
