"""
ArqTaskAdapter unit tests.

Verifies the arq-based async task queue adapter with mocked arq dependencies.
All arq/Redis interactions are mocked — no external infrastructure needed.

Test Categories:
    A. Contract: provider_name, traceback truncation constant
    B. Behavior: task registration, enqueue, get_result, revoke,
       health_check, worker settings, lifecycle, error handling
"""

from __future__ import annotations

import asyncio
import sys
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# arq is an optional dependency — install fake modules for unit testing
_arq_mock = ModuleType("arq")
_arq_jobs_mock = ModuleType("arq.jobs")
_arq_connections_mock = ModuleType("arq.connections")
_arq_cron_mock = ModuleType("arq.cron")

# Create mock classes that will be used by the adapter
_MockJob = MagicMock(name="Job")
_MockCreatePool = AsyncMock(name="create_pool")

_arq_mock.create_pool = _MockCreatePool
_arq_mock.ArqRedis = MagicMock(name="ArqRedis")
_arq_jobs_mock.Job = _MockJob
_arq_connections_mock.RedisSettings = MagicMock(name="RedisSettings")
_arq_cron_mock.cron = MagicMock(name="cron")
_arq_cron_mock.CronJob = MagicMock(name="CronJob")

# Install into sys.modules before importing the adapter
for _name, _mod in [
    ("arq", _arq_mock),
    ("arq.jobs", _arq_jobs_mock),
    ("arq.connections", _arq_connections_mock),
    ("arq.cron", _arq_cron_mock),
]:
    if _name not in sys.modules:
        sys.modules[_name] = _mod

from baldur.adapters.queues.arq_adapter import (
    _MAX_TRACEBACK_LENGTH,
    ArqTaskAdapter,
)
from baldur.interfaces.task_queue import (
    PartialEnqueueError,
    TaskNotFoundError,
    TaskOptions,
    TaskStatus,
    TaskTimeoutError,
)
from baldur.settings.arq_task import reset_arq_task_settings

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def adapter():
    """ArqTaskAdapter with no Redis settings (constructor is sync)."""
    return ArqTaskAdapter()


@pytest.fixture
def mock_pool():
    """Mock ArqRedis pool."""
    pool = AsyncMock()
    pool.ping = AsyncMock(return_value=True)
    pool.close = AsyncMock()
    pool.zcard = AsyncMock(return_value=5)
    return pool


@pytest.fixture
def adapter_with_pool(adapter, mock_pool):
    """ArqTaskAdapter with a mocked pool (simulates post-startup state)."""
    adapter._pool = mock_pool
    return adapter


# =============================================================================
# A. Contract Tests
# =============================================================================


class TestArqTaskAdapterContract:
    """ArqTaskAdapter design contract verification."""

    def test_provider_name_is_arq(self, adapter):
        """provider_name returns 'arq'."""
        assert adapter.provider_name == "arq"

    def test_max_traceback_length_constant(self):
        """_MAX_TRACEBACK_LENGTH is 4096 (design contract §14.1)."""
        assert _MAX_TRACEBACK_LENGTH == 4096

    def test_constructor_is_sync_and_does_no_io(self):
        """Constructor completes without any async I/O."""
        adapter = ArqTaskAdapter()
        assert adapter._pool is None
        assert adapter._registered_tasks == {}
        assert adapter._cron_jobs == []


# =============================================================================
# B. Behavior Tests
# =============================================================================


class TestArqTaskRegistrationBehavior:
    """Task registration via decorator."""

    def test_task_decorator_registers_function(self, adapter):
        """@adapter.task() registers the function in _registered_tasks."""

        @adapter.task(name="my_task")
        async def my_task():
            pass

        assert "my_task" in adapter._registered_tasks
        assert adapter._registered_tasks["my_task"] is my_task

    def test_task_decorator_auto_generates_name(self, adapter):
        """task() without name uses module.qualname as key."""

        @adapter.task()
        async def auto_named():
            pass

        expected_name = f"{auto_named.__module__}.{auto_named.__qualname__}"
        assert expected_name in adapter._registered_tasks

    def test_task_decorator_sets_metadata(self, adapter):
        """task() stores max_retries, timeout, queue as function attributes."""

        @adapter.task(name="t", max_retries=5, timeout=120, queue="high")
        async def t():
            pass

        assert t._task_name == "t"
        assert t._max_retries == 5
        assert t._timeout == 120
        assert t._queue == "high"

    def test_task_decorator_returns_original_function(self, adapter):
        """Decorated function is the same object (no wrapping)."""

        @adapter.task(name="orig")
        async def orig():
            return 42

        assert adapter._registered_tasks["orig"] is orig


class TestArqEnqueueBehavior:
    """Task enqueue operations."""

    @pytest.mark.asyncio
    async def test_enqueue_unknown_task_raises_task_not_found(self, adapter_with_pool):
        """Enqueueing an unregistered task raises TaskNotFoundError."""
        with pytest.raises(TaskNotFoundError, match="Unknown task"):
            await adapter_with_pool.enqueue("nonexistent_task")

    @pytest.mark.asyncio
    async def test_enqueue_returns_job_id(self, adapter_with_pool, mock_pool):
        """Successful enqueue returns the arq job ID."""
        # Given
        mock_job = MagicMock()
        mock_job.job_id = "job-123"
        mock_pool.enqueue_job = AsyncMock(return_value=mock_job)

        @adapter_with_pool.task(name="test_task")
        async def test_task():
            pass

        # When
        result = await adapter_with_pool.enqueue("test_task", args=(1,))

        # Then
        assert result == "job-123"
        mock_pool.enqueue_job.assert_called_once()

    @pytest.mark.asyncio
    async def test_enqueue_passes_options_to_arq(self, adapter_with_pool, mock_pool):
        """TaskOptions are forwarded to arq enqueue_job."""
        mock_job = MagicMock()
        mock_job.job_id = "job-456"
        mock_pool.enqueue_job = AsyncMock(return_value=mock_job)

        @adapter_with_pool.task(name="opt_task")
        async def opt_task():
            pass

        options = TaskOptions(queue="priority", countdown=30)
        await adapter_with_pool.enqueue("opt_task", options=options)

        call_kwargs = mock_pool.enqueue_job.call_args
        assert call_kwargs.kwargs.get("_queue_name") == "priority"

    @pytest.mark.asyncio
    async def test_enqueue_none_job_raises_error(self, adapter_with_pool, mock_pool):
        """When arq returns None (duplicate/expired), raises TaskNotFoundError."""
        mock_pool.enqueue_job = AsyncMock(return_value=None)

        @adapter_with_pool.task(name="dup_task")
        async def dup_task():
            pass

        with pytest.raises(TaskNotFoundError, match="Failed to enqueue"):
            await adapter_with_pool.enqueue("dup_task")

    @pytest.mark.asyncio
    async def test_enqueue_many_returns_all_ids(self, adapter_with_pool, mock_pool):
        """enqueue_many returns a list of job IDs."""
        call_count = 0

        async def mock_enqueue(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            job = MagicMock()
            job.job_id = f"job-{call_count}"
            return job

        mock_pool.enqueue_job = mock_enqueue

        @adapter_with_pool.task(name="batch_task")
        async def batch_task():
            pass

        tasks = [
            ("batch_task", (1,), {}),
            ("batch_task", (2,), {}),
            ("batch_task", (3,), {}),
        ]
        result = await adapter_with_pool.enqueue_many(tasks)
        assert result == ["job-1", "job-2", "job-3"]


class TestArqEnqueueManyBehavior:
    """enqueue_many chunked gather behavior (§344)."""

    @pytest.fixture(autouse=True)
    def _reset_settings(self):
        """Ensure clean settings state for each test."""
        reset_arq_task_settings()
        yield
        reset_arq_task_settings()

    def _make_adapter_with_mock_enqueue(
        self, mock_pool, succeed_ids=None, fail_at=None
    ):
        """Create adapter with mock enqueue returning sequential IDs.

        Args:
            mock_pool: Mock ArqRedis pool.
            succeed_ids: If given, use these IDs. Otherwise auto-generate.
            fail_at: Set of indices (0-based) that should raise an exception.
        """
        adapter = ArqTaskAdapter()
        adapter._pool = mock_pool

        @adapter.task(name="t")
        async def t():
            pass

        call_count = 0
        fail_at = fail_at or set()

        async def mock_enqueue_job(*args, **kwargs):
            nonlocal call_count
            idx = call_count
            call_count += 1
            if idx in fail_at:
                raise RuntimeError(f"enqueue failed at {idx}")
            job = MagicMock()
            if succeed_ids:
                job.job_id = succeed_ids[idx] if idx < len(succeed_ids) else f"id-{idx}"
            else:
                job.job_id = f"id-{idx}"
            return job

        mock_pool.enqueue_job = mock_enqueue_job
        return adapter

    @pytest.mark.asyncio
    async def test_enqueue_many_empty_list_returns_empty(self, mock_pool):
        """Empty task list returns empty result without calling enqueue."""
        adapter = self._make_adapter_with_mock_enqueue(mock_pool)
        result = await adapter.enqueue_many([])
        assert result == []

    @pytest.mark.asyncio
    async def test_enqueue_many_single_task_succeeds(self, mock_pool):
        """Single-task list returns one ID."""
        adapter = self._make_adapter_with_mock_enqueue(mock_pool)
        result = await adapter.enqueue_many([("t", (), {})])
        assert len(result) == 1
        assert result[0] == "id-0"

    @pytest.mark.asyncio
    async def test_enqueue_many_all_succeed_returns_ordered_ids(self, mock_pool):
        """All tasks succeed — returns IDs in input order."""
        adapter = self._make_adapter_with_mock_enqueue(mock_pool)
        tasks = [("t", (), {}) for _ in range(5)]
        result = await adapter.enqueue_many(tasks)
        assert result == [f"id-{i}" for i in range(5)]

    @pytest.mark.asyncio
    async def test_enqueue_many_partial_failure_raises_partial_enqueue_error(
        self, mock_pool
    ):
        """Some failures raise PartialEnqueueError with succeeded and failed."""
        # Given — 5 tasks, indices 1 and 3 fail
        adapter = self._make_adapter_with_mock_enqueue(mock_pool, fail_at={1, 3})

        # When / Then
        with pytest.raises(PartialEnqueueError) as exc_info:
            await adapter.enqueue_many([("t", (), {}) for _ in range(5)])

        err = exc_info.value
        assert len(err.succeeded) == 3
        assert len(err.failed) == 2

    @pytest.mark.asyncio
    async def test_enqueue_many_partial_failure_preserves_original_indices(
        self, mock_pool
    ):
        """failed tuples contain the original index in the tasks list."""
        adapter = self._make_adapter_with_mock_enqueue(mock_pool, fail_at={1, 3})

        with pytest.raises(PartialEnqueueError) as exc_info:
            await adapter.enqueue_many([("t", (), {}) for _ in range(5)])

        failed_indices = {idx for idx, _ in exc_info.value.failed}
        assert failed_indices == {1, 3}

    @pytest.mark.asyncio
    async def test_enqueue_many_partial_failure_succeeded_preserves_indices(
        self, mock_pool
    ):
        """succeeded tuples contain (original_index, task_id) pairs."""
        adapter = self._make_adapter_with_mock_enqueue(mock_pool, fail_at={1, 3})

        with pytest.raises(PartialEnqueueError) as exc_info:
            await adapter.enqueue_many([("t", (), {}) for _ in range(5)])

        succeeded = exc_info.value.succeeded
        succeeded_indices = {idx for idx, _ in succeeded}
        assert succeeded_indices == {0, 2, 4}
        for idx, task_id in succeeded:
            assert task_id == f"id-{idx}"

    @pytest.mark.asyncio
    async def test_enqueue_many_all_fail_raises_with_empty_succeeded(self, mock_pool):
        """All tasks fail — PartialEnqueueError.succeeded is empty."""
        adapter = self._make_adapter_with_mock_enqueue(mock_pool, fail_at={0, 1, 2})

        with pytest.raises(PartialEnqueueError) as exc_info:
            await adapter.enqueue_many([("t", (), {}) for _ in range(3)])

        err = exc_info.value
        assert err.succeeded == []
        assert len(err.failed) == 3

    @pytest.mark.asyncio
    async def test_enqueue_many_chunked_execution_respects_batch_size(self, mock_pool):
        """Tasks exceeding batch_size are split into multiple gather chunks."""
        # Given — batch_size=10, 25 tasks → 3 chunks (10+10+5)
        adapter = self._make_adapter_with_mock_enqueue(mock_pool)

        with patch.dict(
            "os.environ",
            {"BALDUR_ARQ_TASK_ENQUEUE_BATCH_SIZE": "10"},
        ):
            reset_arq_task_settings()
            result = await adapter.enqueue_many([("t", (), {}) for _ in range(25)])

        assert len(result) == 25
        assert result == [f"id-{i}" for i in range(25)]

    @pytest.mark.asyncio
    async def test_enqueue_many_threshold_abort_skips_remaining_chunks(self, mock_pool):
        """Failure ratio >= threshold in a chunk aborts remaining chunks."""
        # Given — batch_size=10, threshold=0.5
        # First chunk (indices 0-9): 6 fail out of 10 → 60% >= 50% → abort
        # Second chunk (indices 10-19): should NOT be reached
        adapter = self._make_adapter_with_mock_enqueue(
            mock_pool,
            fail_at={0, 1, 2, 3, 4, 5},
        )

        with patch.dict(
            "os.environ",
            {
                "BALDUR_ARQ_TASK_ENQUEUE_BATCH_SIZE": "10",
                "BALDUR_ARQ_TASK_ENQUEUE_FAILURE_THRESHOLD": "0.5",
            },
        ):
            reset_arq_task_settings()

            with pytest.raises(PartialEnqueueError) as exc_info:
                await adapter.enqueue_many([("t", (), {}) for _ in range(20)])

        err = exc_info.value
        # Only first chunk was processed: 4 succeeded + 6 failed = 10
        assert len(err.succeeded) == 4
        assert len(err.failed) == 6
        # Total processed is 10, not 20 — second chunk was skipped
        assert len(err.succeeded) + len(err.failed) == 10

    @pytest.mark.asyncio
    async def test_enqueue_many_below_threshold_continues_next_chunk(self, mock_pool):
        """Failure ratio below threshold continues to next chunk."""
        # Given — batch_size=10, threshold=0.5
        # First chunk (indices 0-9): 2 fail out of 10 → 20% < 50% → continue
        # Second chunk (indices 10-19): all succeed
        adapter = self._make_adapter_with_mock_enqueue(
            mock_pool,
            fail_at={2, 7},
        )

        with patch.dict(
            "os.environ",
            {
                "BALDUR_ARQ_TASK_ENQUEUE_BATCH_SIZE": "10",
                "BALDUR_ARQ_TASK_ENQUEUE_FAILURE_THRESHOLD": "0.5",
            },
        ):
            reset_arq_task_settings()

            with pytest.raises(PartialEnqueueError) as exc_info:
                await adapter.enqueue_many([("t", (), {}) for _ in range(20)])

        err = exc_info.value
        # Both chunks processed: 18 succeeded + 2 failed = 20
        assert len(err.succeeded) == 18
        assert len(err.failed) == 2
        assert len(err.succeeded) + len(err.failed) == 20

    @pytest.mark.asyncio
    async def test_enqueue_many_cancelled_error_propagates_immediately(self, mock_pool):
        """CancelledError is re-raised, not treated as a normal failure."""
        adapter = ArqTaskAdapter()
        adapter._pool = mock_pool

        @adapter.task(name="t")
        async def t():
            pass

        call_count = 0

        async def mock_enqueue_job(*args, **kwargs):
            nonlocal call_count
            idx = call_count
            call_count += 1
            if idx == 2:
                raise asyncio.CancelledError()
            job = MagicMock()
            job.job_id = f"id-{idx}"
            return job

        mock_pool.enqueue_job = mock_enqueue_job

        with pytest.raises(asyncio.CancelledError):
            await adapter.enqueue_many([("t", (), {}) for _ in range(5)])

    @pytest.mark.asyncio
    async def test_enqueue_many_cancelled_error_not_in_partial_enqueue(self, mock_pool):
        """CancelledError does not appear in PartialEnqueueError.failed."""
        adapter = ArqTaskAdapter()
        adapter._pool = mock_pool

        @adapter.task(name="t")
        async def t():
            pass

        call_count = 0

        async def mock_enqueue_job(*args, **kwargs):
            nonlocal call_count
            idx = call_count
            call_count += 1
            if idx == 1:
                raise asyncio.CancelledError()
            job = MagicMock()
            job.job_id = f"id-{idx}"
            return job

        mock_pool.enqueue_job = mock_enqueue_job

        # CancelledError should propagate, NOT be wrapped in PartialEnqueueError
        with pytest.raises(asyncio.CancelledError):
            await adapter.enqueue_many([("t", (), {}) for _ in range(3)])

    @pytest.mark.asyncio
    async def test_enqueue_many_threshold_zero_aborts_on_any_failure(self, mock_pool):
        """threshold=0.0 aborts on any failure in the chunk."""
        # Given — batch_size=10, threshold=0.0
        # First chunk: 1 fail out of 10 → 10% >= 0% → abort
        adapter = self._make_adapter_with_mock_enqueue(mock_pool, fail_at={5})

        with patch.dict(
            "os.environ",
            {
                "BALDUR_ARQ_TASK_ENQUEUE_BATCH_SIZE": "10",
                "BALDUR_ARQ_TASK_ENQUEUE_FAILURE_THRESHOLD": "0.0",
            },
        ):
            reset_arq_task_settings()

            with pytest.raises(PartialEnqueueError) as exc_info:
                await adapter.enqueue_many([("t", (), {}) for _ in range(20)])

        err = exc_info.value
        # Only first chunk processed (10 tasks), second chunk skipped
        assert len(err.succeeded) + len(err.failed) == 10
        assert len(err.failed) == 1

    @pytest.mark.asyncio
    async def test_enqueue_many_threshold_one_aborts_only_on_total_failure(
        self, mock_pool
    ):
        """threshold=1.0 aborts only when 100% of chunk fails."""
        # Given — batch_size=10, threshold=1.0
        # First chunk: 5 fail out of 10 → 50% < 100% → continue
        adapter = self._make_adapter_with_mock_enqueue(
            mock_pool, fail_at={0, 1, 2, 3, 4}
        )

        with patch.dict(
            "os.environ",
            {
                "BALDUR_ARQ_TASK_ENQUEUE_BATCH_SIZE": "10",
                "BALDUR_ARQ_TASK_ENQUEUE_FAILURE_THRESHOLD": "1.0",
            },
        ):
            reset_arq_task_settings()

            with pytest.raises(PartialEnqueueError) as exc_info:
                await adapter.enqueue_many([("t", (), {}) for _ in range(20)])

        err = exc_info.value
        # Both chunks processed: 5 failed in chunk 1, 0 in chunk 2 = 20 total
        assert len(err.succeeded) + len(err.failed) == 20
        assert len(err.failed) == 5

    @pytest.mark.asyncio
    async def test_enqueue_many_concurrent_execution_is_faster_than_sequential(
        self, mock_pool
    ):
        """gather executes concurrently — total time < sum of individual delays."""
        adapter = ArqTaskAdapter()
        adapter._pool = mock_pool

        @adapter.task(name="slow")
        async def slow():
            pass

        # Each enqueue sleeps 0.05s; 5 tasks sequentially = 0.25s
        call_count = 0

        async def slow_enqueue(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            await asyncio.sleep(0.05)
            job = MagicMock()
            job.job_id = f"id-{call_count}"
            return job

        mock_pool.enqueue_job = slow_enqueue

        import time

        start = time.perf_counter()
        result = await adapter.enqueue_many([("slow", (), {}) for _ in range(5)])
        elapsed = time.perf_counter() - start

        assert len(result) == 5
        # Concurrent: should be much less than 0.25s (sequential total)
        assert elapsed < 0.15


class TestArqPoolLifecycleBehavior:
    """Pool initialization and lifecycle."""

    @pytest.mark.asyncio
    async def test_enqueue_without_startup_raises_runtime_error(self, adapter):
        """Enqueueing before startup() raises RuntimeError."""

        @adapter.task(name="pre_startup")
        async def pre_startup():
            pass

        with pytest.raises(RuntimeError, match="pool not initialized"):
            await adapter.enqueue("pre_startup")

    @pytest.mark.asyncio
    async def test_shutdown_without_startup_is_safe(self, adapter):
        """shutdown() without prior startup() is a no-op."""
        await adapter.shutdown()

    @pytest.mark.asyncio
    async def test_shutdown_closes_pool(self, adapter_with_pool, mock_pool):
        """shutdown() closes the Redis pool."""
        await adapter_with_pool.shutdown()
        mock_pool.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_startup_creates_pool(self, adapter):
        """startup() creates a Redis pool via arq.create_pool."""
        mock_pool = AsyncMock()
        with patch(
            "arq.create_pool",
            new=AsyncMock(return_value=mock_pool),
        ) as mock_create:
            await adapter.startup()

        assert adapter._pool is mock_pool
        mock_create.assert_called_once()


class TestArqGetResultBehavior:
    """Task result retrieval and status mapping."""

    @pytest.mark.asyncio
    async def test_get_result_job_not_found_returns_pending(
        self, adapter_with_pool, mock_pool
    ):
        """Unknown job ID returns PENDING status."""
        with patch("arq.jobs.Job") as MockJob:
            mock_job_instance = MagicMock()
            mock_job_instance.info = AsyncMock(return_value=None)
            MockJob.return_value = mock_job_instance

            result = await adapter_with_pool.get_result("unknown-id")

        assert result.task_id == "unknown-id"
        assert result.status == TaskStatus.PENDING

    @pytest.mark.asyncio
    async def test_get_result_maps_complete_to_success(
        self, adapter_with_pool, mock_pool
    ):
        """arq 'complete' status maps to TaskStatus.SUCCESS."""
        info = MagicMock()
        info.status = "complete"
        info.result = {"data": 42}
        info.start_time = None
        info.finish_time = None

        with patch("arq.jobs.Job") as MockJob:
            mock_job_instance = MagicMock()
            mock_job_instance.info = AsyncMock(return_value=info)
            MockJob.return_value = mock_job_instance

            result = await adapter_with_pool.get_result("job-ok")

        assert result.status == TaskStatus.SUCCESS
        assert result.result == {"data": 42}

    @pytest.mark.asyncio
    async def test_get_result_truncates_long_traceback(
        self, adapter_with_pool, mock_pool
    ):
        """Error traceback exceeding _MAX_TRACEBACK_LENGTH is truncated."""
        # Given — an exception with a very long message
        long_msg = "x" * (_MAX_TRACEBACK_LENGTH + 1000)
        error = ValueError(long_msg)

        info = MagicMock()
        info.status = "not_a_known_status"  # maps to FAILURE
        info.result = error
        info.start_time = None
        info.finish_time = None

        with patch("arq.jobs.Job") as MockJob:
            mock_job_instance = MagicMock()
            mock_job_instance.info = AsyncMock(return_value=info)
            MockJob.return_value = mock_job_instance

            result = await adapter_with_pool.get_result("job-fail")

        # Then
        assert result.status == TaskStatus.FAILURE
        assert len(result.error) <= _MAX_TRACEBACK_LENGTH

    @pytest.mark.asyncio
    async def test_get_result_with_timeout_waits_for_completion(
        self, adapter_with_pool, mock_pool
    ):
        """get_result with timeout waits for job.result() before fetching info."""
        info = MagicMock()
        info.status = "complete"
        info.result = "done"
        info.start_time = None
        info.finish_time = None

        with patch("arq.jobs.Job") as MockJob:
            mock_job_instance = MagicMock()
            mock_job_instance.result = AsyncMock(return_value="done")
            mock_job_instance.info = AsyncMock(return_value=info)
            MockJob.return_value = mock_job_instance

            result = await adapter_with_pool.get_result("job-wait", timeout=5.0)

        assert result.status == TaskStatus.SUCCESS
        assert result.result == "done"
        mock_job_instance.result.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_result_with_timeout_raises_on_expiry(
        self, adapter_with_pool, mock_pool
    ):
        """get_result raises TaskTimeoutError when timeout expires."""
        with patch("arq.jobs.Job") as MockJob:
            mock_job_instance = MagicMock()
            # Simulate a job that never completes
            mock_job_instance.result = AsyncMock(side_effect=TimeoutError())
            MockJob.return_value = mock_job_instance

            with pytest.raises(TaskTimeoutError, match="did not complete"):
                await adapter_with_pool.get_result("job-slow", timeout=0.01)

    @pytest.mark.asyncio
    async def test_get_result_with_timeout_handles_task_exception(
        self, adapter_with_pool, mock_pool
    ):
        """get_result with timeout handles job exception gracefully."""
        info = MagicMock()
        info.status = "not_a_known_status"  # maps to FAILURE
        info.result = ValueError("task failed")
        info.start_time = None
        info.finish_time = None

        with patch("arq.jobs.Job") as MockJob:
            mock_job_instance = MagicMock()
            # arq re-raises the task's exception from result()
            mock_job_instance.result = AsyncMock(side_effect=ValueError("task failed"))
            mock_job_instance.info = AsyncMock(return_value=info)
            MockJob.return_value = mock_job_instance

            result = await adapter_with_pool.get_result("job-err", timeout=5.0)

        assert result.status == TaskStatus.FAILURE

    @pytest.mark.asyncio
    async def test_get_result_without_timeout_does_not_wait(
        self, adapter_with_pool, mock_pool
    ):
        """get_result without timeout returns immediately (no job.result() call)."""
        info = MagicMock()
        info.status = "in_progress"
        info.result = None
        info.start_time = None
        info.finish_time = None

        with patch("arq.jobs.Job") as MockJob:
            mock_job_instance = MagicMock()
            mock_job_instance.result = AsyncMock()
            mock_job_instance.info = AsyncMock(return_value=info)
            MockJob.return_value = mock_job_instance

            result = await adapter_with_pool.get_result("job-progress")

        assert result.status == TaskStatus.STARTED
        mock_job_instance.result.assert_not_called()

    def test_map_status_known_values(self):
        """_map_status maps all known arq statuses correctly."""
        assert ArqTaskAdapter._map_status("deferred") == TaskStatus.PENDING
        assert ArqTaskAdapter._map_status("queued") == TaskStatus.PENDING
        assert ArqTaskAdapter._map_status("in_progress") == TaskStatus.STARTED
        assert ArqTaskAdapter._map_status("complete") == TaskStatus.SUCCESS
        assert ArqTaskAdapter._map_status("not_found") == TaskStatus.PENDING

    def test_map_status_unknown_defaults_to_failure(self):
        """_map_status returns FAILURE for unknown arq statuses."""
        assert ArqTaskAdapter._map_status("weird_status") == TaskStatus.FAILURE
        assert ArqTaskAdapter._map_status(None) == TaskStatus.FAILURE


class TestArqHealthCheckBehavior:
    """Health check operations."""

    @pytest.mark.asyncio
    async def test_health_check_success(self, adapter_with_pool, mock_pool):
        """health_check returns True when Redis ping succeeds."""
        mock_pool.ping = AsyncMock(return_value=True)
        assert await adapter_with_pool.health_check() is True

    @pytest.mark.asyncio
    async def test_health_check_connection_error_returns_false(
        self, adapter_with_pool, mock_pool
    ):
        """health_check returns False on connection error (no exception propagation)."""
        mock_pool.ping = AsyncMock(side_effect=ConnectionError("Redis down"))
        assert await adapter_with_pool.health_check() is False

    @pytest.mark.asyncio
    async def test_health_check_without_pool_returns_false(self, adapter):
        """health_check without startup returns False (pool not initialized)."""
        assert await adapter.health_check() is False


class TestArqRevokeBehavior:
    """Task revocation operations."""

    @pytest.mark.asyncio
    async def test_revoke_forwards_abort_return_value_true(
        self, adapter_with_pool, mock_pool
    ):
        """revoke() returns True when Job.abort() returns True."""
        with patch("arq.jobs.Job") as MockJob:
            mock_job_instance = MagicMock()
            mock_job_instance.abort = AsyncMock(return_value=True)
            MockJob.return_value = mock_job_instance

            result = await adapter_with_pool.revoke("job-to-cancel")

        assert result is True
        mock_job_instance.abort.assert_called_once()

    @pytest.mark.asyncio
    async def test_revoke_forwards_abort_return_value_false(
        self, adapter_with_pool, mock_pool
    ):
        """revoke() returns False when Job.abort() returns False (already completed)."""
        with patch("arq.jobs.Job") as MockJob:
            mock_job_instance = MagicMock()
            mock_job_instance.abort = AsyncMock(return_value=False)
            MockJob.return_value = mock_job_instance

            result = await adapter_with_pool.revoke("job-already-done")

        assert result is False


class TestArqQueueManagementBehavior:
    """Queue management operations."""

    @pytest.mark.asyncio
    async def test_queue_length_returns_zcard(self, adapter_with_pool, mock_pool):
        """queue_length delegates to Redis ZCARD."""
        mock_pool.zcard = AsyncMock(return_value=42)
        assert await adapter_with_pool.queue_length("arq:queue") == 42
        mock_pool.zcard.assert_awaited_once_with("arq:queue")


class TestArqWorkerSettingsBehavior:
    """Worker settings generation."""

    def test_get_worker_settings_returns_registered_tasks(self, adapter):
        """get_worker_settings includes registered functions."""

        @adapter.task(name="worker_task")
        async def worker_task():
            pass

        settings = adapter.get_worker_settings()
        assert worker_task in settings["functions"]
        assert settings["cron_jobs"] == []
        assert settings["redis_settings"] is None

    def test_get_worker_settings_includes_redis_settings(self):
        """get_worker_settings includes the redis_settings passed at init."""
        mock_settings = MagicMock()
        adapter = ArqTaskAdapter(redis_settings=mock_settings)
        settings = adapter.get_worker_settings()
        assert settings["redis_settings"] is mock_settings
