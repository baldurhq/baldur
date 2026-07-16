"""
AsyncSemaphoreBulkhead unit tests.

Verify the behavior of the async-semaphore-based bulkhead:
- Async acquire/release behavior
- Maximum concurrency limit
- asyncio.wait_for-based timeout
- try_acquire timed-wait honoring + rejection-stats contract (D3, D6)
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from baldur.services.bulkhead.async_semaphore import AsyncSemaphoreBulkhead
from baldur.services.bulkhead.base import BulkheadState, BulkheadType
from baldur.services.bulkhead.exceptions import BulkheadFullError


class TestAsyncSemaphoreBulkheadBasic:
    """Basic behavior tests."""

    @pytest.mark.asyncio
    async def test_create_bulkhead_with_default_values(self):
        """Create a bulkhead with default values."""
        bulkhead = AsyncSemaphoreBulkhead("test")

        assert bulkhead.name == "test"
        state = bulkhead.get_state()
        assert state.max_concurrent == 10
        assert state.active_count == 0
        assert state.bulkhead_type == BulkheadType.SEMAPHORE

    @pytest.mark.asyncio
    async def test_acquire_and_release(self):
        """Acquire then release."""
        bulkhead = AsyncSemaphoreBulkhead("test", max_concurrent=2)

        async with bulkhead.acquire():
            state = bulkhead.get_state()
            assert state.active_count == 1

        state = bulkhead.get_state()
        assert state.active_count == 0

    @pytest.mark.asyncio
    async def test_nested_acquire(self):
        """Nested acquire."""
        bulkhead = AsyncSemaphoreBulkhead("test", max_concurrent=3)

        async with bulkhead.acquire():
            async with bulkhead.acquire():
                state = bulkhead.get_state()
                assert state.active_count == 2

            state = bulkhead.get_state()
            assert state.active_count == 1

        state = bulkhead.get_state()
        assert state.active_count == 0


class TestAsyncSemaphoreBulkheadConcurrency:
    """Concurrency limit tests."""

    @pytest.mark.asyncio
    async def test_reject_when_full_no_timeout(self):
        """Reject immediately when the bulkhead is full (timeout=None)."""
        bulkhead = AsyncSemaphoreBulkhead("test", max_concurrent=1)

        async with bulkhead.acquire():
            with pytest.raises(BulkheadFullError) as exc_info:
                async with bulkhead.acquire():
                    pass

            assert exc_info.value.bulkhead_name == "test"
            assert exc_info.value.max_concurrent == 1

    @pytest.mark.asyncio
    async def test_reject_increments_counter(self):
        """Rejection increments the counter."""
        bulkhead = AsyncSemaphoreBulkhead("test", max_concurrent=1)

        async with bulkhead.acquire():
            for _ in range(3):
                with pytest.raises(BulkheadFullError):
                    async with bulkhead.acquire():
                        pass

        state = bulkhead.get_state()
        assert state.rejected_count == 3
        assert state.last_rejection_time is not None

    @pytest.mark.asyncio
    async def test_concurrent_tasks_limited(self):
        """Concurrent task count is limited."""
        bulkhead = AsyncSemaphoreBulkhead("test", max_concurrent=3)
        max_concurrent_observed = 0
        lock = asyncio.Lock()

        async def worker():
            nonlocal max_concurrent_observed
            try:
                async with bulkhead.acquire(timeout=1.0):
                    async with lock:
                        current = bulkhead.get_state().active_count
                        if current > max_concurrent_observed:
                            max_concurrent_observed = current
                    await asyncio.sleep(0.05)
            except BulkheadFullError:
                pass

        # Run 10 tasks concurrently
        tasks = [asyncio.create_task(worker()) for _ in range(10)]
        await asyncio.gather(*tasks)

        # The maximum concurrency must not exceed 3
        assert max_concurrent_observed <= 3


class TestAsyncSemaphoreBulkheadTimeout:
    """Timeout tests."""

    @pytest.mark.asyncio
    async def test_acquire_with_timeout_success(self):
        """Acquire succeeds within the timeout."""
        bulkhead = AsyncSemaphoreBulkhead("test", max_concurrent=1)

        # Release after a short delay in the background
        async def release_after_delay():
            await asyncio.sleep(0.1)
            await bulkhead.release()

        # First acquisition
        acquired = await bulkhead.try_acquire()
        assert acquired

        # Start the background task
        release_task = asyncio.create_task(release_after_delay())

        # Attempt acquisition within the timeout
        async with bulkhead.acquire(timeout=1.0):
            pass

        await release_task

    @pytest.mark.asyncio
    async def test_acquire_with_timeout_failure(self):
        """Fail when the timeout is exceeded."""
        bulkhead = AsyncSemaphoreBulkhead("test", max_concurrent=1)

        async with bulkhead.acquire():
            with pytest.raises(BulkheadFullError):
                async with bulkhead.acquire(timeout=0.05):
                    pass


class TestAsyncSemaphoreBulkheadState:
    """State query tests."""

    @pytest.mark.asyncio
    async def test_get_state_returns_correct_values(self):
        """State query returns the correct values."""
        bulkhead = AsyncSemaphoreBulkhead("test", max_concurrent=5)

        state = bulkhead.get_state()
        assert isinstance(state, BulkheadState)
        assert state.name == "test"
        assert state.bulkhead_type == BulkheadType.SEMAPHORE
        assert state.max_concurrent == 5

    @pytest.mark.asyncio
    async def test_utilization_percent_calculation(self):
        """Utilization calculation."""
        bulkhead = AsyncSemaphoreBulkhead("test", max_concurrent=4)

        state = bulkhead.get_state()
        assert state.utilization_percent == 0.0

        async with bulkhead.acquire():
            async with bulkhead.acquire():
                state = bulkhead.get_state()
                assert state.utilization_percent == 50.0  # 2/4 = 50%


class TestAsyncSemaphoreBulkheadTryAcquire:
    """try_acquire method tests — timed-wait honoring (D3) + rejection stats (D6).

    try_acquire mirrors this class's ``acquire`` timeout contract: None is an
    immediate verdict; a timeout waits up to that bound via asyncio.wait_for.
    Both failure paths record rejection stats.
    """

    @pytest.mark.asyncio
    async def test_try_acquire_success(self):
        """try_acquire succeeds on a free slot."""
        bulkhead = AsyncSemaphoreBulkhead("test", max_concurrent=2)

        result = await bulkhead.try_acquire()
        assert result is True
        state = bulkhead.get_state()
        assert state.active_count == 1

        # Cleanup
        await bulkhead.release()

    @pytest.mark.asyncio
    async def test_try_acquire_none_timeout_fails_immediately_and_records_stats(self):
        """try_acquire(timeout=None) fails immediately when full and records the
        rejection (immediate failure path, D3/D6)."""
        # Given — the only slot is occupied
        bulkhead = AsyncSemaphoreBulkhead("test", max_concurrent=1)
        assert await bulkhead.try_acquire() is True

        # When — a non-blocking attempt against the full bulkhead
        result = await bulkhead.try_acquire(timeout=None)

        # Then — rejected and recorded
        assert result is False
        state = bulkhead.get_state()
        assert state.rejected_count == 1
        assert state.last_rejection_time is not None

        await bulkhead.release()

    @pytest.mark.asyncio
    async def test_try_acquire_timed_wait_succeeds_after_concurrent_release(self):
        """A timed try_acquire waits and succeeds once a slot is released
        concurrently (timeout is honored via asyncio.wait_for, D3)."""
        # Given — the only slot is occupied
        bulkhead = AsyncSemaphoreBulkhead("test", max_concurrent=1)
        assert await bulkhead.try_acquire() is True

        async def release_after_delay():
            await asyncio.sleep(0.02)
            await bulkhead.release()

        release_task = asyncio.create_task(release_after_delay())

        # When — the timed attempt waits for the release, then succeeds
        result = await bulkhead.try_acquire(timeout=1.0)

        # Then — acquired, no rejection recorded
        assert result is True
        state = bulkhead.get_state()
        assert state.rejected_count == 0

        await release_task
        await bulkhead.release()

    @pytest.mark.asyncio
    async def test_try_acquire_timed_wait_expires_returns_false_and_records_stats(self):
        """A timed try_acquire that never gets a slot expires to False and records
        the rejection (timed-expiry failure path, D3/D6)."""
        # Given — the only slot is occupied with no release pending
        bulkhead = AsyncSemaphoreBulkhead("test", max_concurrent=1)
        assert await bulkhead.try_acquire() is True

        # When — a timed attempt that cannot be satisfied
        result = await bulkhead.try_acquire(timeout=0.05)

        # Then — expires to False and the rejection is recorded
        assert result is False
        state = bulkhead.get_state()
        assert state.rejected_count == 1
        assert state.last_rejection_time is not None

        await bulkhead.release()


class TestAsyncSemaphoreBulkheadRejectionMetric:
    """644 D3: each reject path increments baldur_bulkhead_rejected_total via
    increment_rejected_count, emitted *outside* self._lock.

    self._lock is an asyncio.Lock; emitting under it while the prometheus client
    takes its own lock would nest two locks. Asserting lock.locked() is False at
    emit time proves the call site is outside the critical section; called_once
    proves the +1 wiring (the never-populated series 644 D3 wired)."""

    @pytest.mark.asyncio
    async def test_acquire_rejection_emits_counter_outside_lock(self):
        """acquire() rejection emits the counter once, outside self._lock."""
        bulkhead = AsyncSemaphoreBulkhead("test", max_concurrent=1)
        lock_held_at_emit: list[bool] = []

        with patch(
            "baldur.services.bulkhead.async_semaphore.increment_rejected_count",
            autospec=True,
            side_effect=lambda name: lock_held_at_emit.append(bulkhead._lock.locked()),
        ) as mock_inc:
            async with bulkhead.acquire():
                with pytest.raises(BulkheadFullError):
                    async with bulkhead.acquire():
                        pass

        mock_inc.assert_called_once_with("test")
        assert lock_held_at_emit == [False]

    @pytest.mark.asyncio
    async def test_try_acquire_rejection_emits_counter_outside_lock(self):
        """try_acquire() rejection emits the counter once, outside self._lock."""
        bulkhead = AsyncSemaphoreBulkhead("test", max_concurrent=1)
        lock_held_at_emit: list[bool] = []

        with patch(
            "baldur.services.bulkhead.async_semaphore.increment_rejected_count",
            autospec=True,
            side_effect=lambda name: lock_held_at_emit.append(bulkhead._lock.locked()),
        ) as mock_inc:
            assert await bulkhead.try_acquire() is True
            assert await bulkhead.try_acquire(timeout=None) is False

        mock_inc.assert_called_once_with("test")
        assert lock_held_at_emit == [False]
