"""
RQ (Redis Queue) Task Adapter for the baldur system.

Implements TaskQueueInterface using RQ as the task queue backend.
RQ is a simple, lightweight, Python library for queueing jobs and processing them in the background with workers.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any, TypeVar

import structlog

from baldur.core.backoff import ExponentialBackoff
from baldur.interfaces.task_queue import (
    TaskOptions,
    TaskQueueInterface,
    TaskResult,
    TaskStatus,
)

logger = structlog.get_logger()
F = TypeVar("F", bound=Callable)

# Celery ``retry_backoff=True`` convention mirrored for RQ: 1 s base, doubling.
_RQ_RETRY_BASE_SECONDS: float = 1.0
_RQ_RETRY_MULTIPLIER: float = 2.0


class RQTaskAdapter(TaskQueueInterface):
    """
    RQ (Redis Queue) implementation of TaskQueueInterface.

    Uses RQ for distributed task processing with Redis as the broker.
    Simpler alternative to Celery for Python-only environments.

    Requirements:
        - rq
        - redis

    Configuration:
        - REDIS_URL: Redis connection URL (default: redis://localhost:6379/0)

    Usage:
        adapter = RQTaskAdapter()

        @adapter.task(max_retries=3)
        def process_payment(payment_id: int):
            # Process payment...
            pass

        # Enqueue task
        task_id = adapter.enqueue("process_payment", args=(123,))

        # Check result
        result = adapter.get_result(task_id)
    """

    def __init__(
        self,
        redis_url: str | None = None,
        default_queue: str = "default",
        default_timeout: int = 3600,
    ):
        """
        Initialize the RQ task adapter.

        Args:
            redis_url: Redis connection URL. If None, reads from settings or uses default.
            default_queue: Default queue name for tasks.
            default_timeout: Default job timeout in seconds.
        """
        self._redis_url = redis_url
        self._default_queue = default_queue
        self._default_timeout = default_timeout
        self._connection = None
        self._queues: dict[str, Any] = {}
        self._tasks: dict[str, dict] = {}
        self._rq = None
        self._redis = None

    @property
    def rq(self):
        """Get RQ module."""
        if self._rq is None:
            try:
                import rq

                self._rq = rq
            except ImportError as _err:
                raise ImportError(
                    "rq is required for RQTaskAdapter. Install it with: pip install rq"
                ) from _err
        return self._rq

    @property
    def redis(self):
        """Get redis module."""
        if self._redis is None:
            try:
                import redis

                self._redis = redis
            except ImportError as err:
                raise ImportError(
                    "redis is required for RQTaskAdapter. "
                    "Install it with: pip install redis"
                ) from err
        return self._redis

    @property
    def connection(self):
        """Get Redis connection."""
        if self._connection is None:
            redis_url = self._redis_url
            if redis_url is None:
                try:
                    from django.conf import settings

                    redis_url = getattr(settings, "REDIS_URL", None)
                except ImportError:
                    pass

            if redis_url is None:
                import os

                redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

            self._connection = self.redis.from_url(redis_url)
        return self._connection

    def _get_queue(self, queue_name: str | None = None) -> Any:
        """Get or create an RQ Queue."""
        name = queue_name or self._default_queue
        if name not in self._queues:
            self._queues[name] = self.rq.Queue(name, connection=self.connection)
        return self._queues[name]

    @property
    def provider_name(self) -> str:
        """Return the provider name."""
        return "rq"

    # =========================================================================
    # Task Registration
    # =========================================================================

    def task(
        self,
        name: str | None = None,
        bind: bool = False,
        max_retries: int = 3,
        autoretry_for: tuple[type[Exception], ...] = (),
        retry_backoff: bool = True,
        retry_backoff_max: int = 600,
        retry_jitter: bool = True,
        rate_limit: str | None = None,
        time_limit: int | None = None,
        soft_time_limit: int | None = None,
    ) -> Callable[[F], F]:
        """
        Decorator to register a function as an RQ task.

        Args:
            name: Task name (default: function name)
            bind: Not used in RQ (included for interface compatibility)
            max_retries: Maximum retry attempts
            autoretry_for: Exception types to auto-retry
            retry_backoff: Use exponential backoff for retries
            retry_backoff_max: Not used in RQ (interface compatibility)
            retry_jitter: Jitter retry intervals (honored on the enqueue path;
                not forwarded from this decorator)
            rate_limit: Not directly supported in RQ
            time_limit: Not used in RQ (interface compatibility)
            soft_time_limit: Not used in RQ (interface compatibility)

        Returns:
            Decorator function
        """

        def decorator(func: F) -> F:
            task_name = name or func.__name__

            # Store task metadata
            self._tasks[task_name] = {
                "func": func,
                "max_retries": max_retries,
                "autoretry_for": autoretry_for,
                "retry_backoff": retry_backoff,
            }

            # Create wrapper with RQ-compatible methods. Typed as Any so the
            # Celery-compatibility attributes (_task_name/delay/apply_async)
            # can be attached without per-attr type-ignore.
            def wrapper_fn(*args: Any, **kwargs: Any) -> Any:
                return func(*args, **kwargs)

            wrapper: Any = wrapper_fn

            wrapper.__name__ = func.__name__
            wrapper.__doc__ = func.__doc__
            wrapper._task_name = task_name

            # Add delay method for Celery compatibility
            def delay(*args: Any, **kwargs: Any) -> RQAsyncResult:
                task_id = self.enqueue(task_name, args=args, kwargs=kwargs)
                return RQAsyncResult(task_id, self)

            wrapper.delay = delay

            # Add apply_async for Celery compatibility
            def apply_async(
                args: tuple = (),
                kwargs: dict | None = None,
                countdown: int | None = None,
                eta: Any = None,
                **options: Any,
            ) -> RQAsyncResult:
                task_options = TaskOptions(countdown=countdown, eta=eta)
                task_id = self.enqueue(
                    task_name,
                    args=args,
                    kwargs=kwargs or {},
                    options=task_options,
                )
                return RQAsyncResult(task_id, self)

            wrapper.apply_async = apply_async

            return wrapper  # type: ignore[no-any-return]

        return decorator

    # =========================================================================
    # Task Execution
    # =========================================================================

    def enqueue(
        self,
        task_name: str,
        args: tuple = (),
        kwargs: dict | None = None,
        options: TaskOptions | None = None,
    ) -> str:
        """
        Enqueue a task for async execution.

        Args:
            task_name: Registered task name
            args: Positional arguments
            kwargs: Keyword arguments
            options: Execution options

        Returns:
            Task ID for tracking
        """
        task_info = self._tasks.get(task_name)
        if task_info is None:
            raise ValueError(f"Task not found: {task_name}")

        func = task_info["func"]
        kwargs = kwargs or {}
        options = options or TaskOptions()

        # Get queue
        queue = self._get_queue(options.queue)

        # Build job options
        job_options = {
            "job_timeout": self._default_timeout,
            "retry": (
                self.rq.Retry(
                    max=task_info.get("max_retries", options.max_retries),
                    interval=(
                        self._get_retry_intervals(options) if options.retry else None
                    ),
                )
                if options.retry
                else None
            ),
        }

        # Wrap enqueue call with retry_with_backoff for transient Redis failures
        def _do_enqueue():
            if options.countdown:
                return queue.enqueue_in(
                    timedelta(seconds=options.countdown),
                    func,
                    *args,
                    **kwargs,
                    **job_options,
                )
            if options.eta:
                return queue.enqueue_at(
                    options.eta,
                    func,
                    *args,
                    **kwargs,
                    **job_options,
                )
            return queue.enqueue(
                func,
                *args,
                **kwargs,
                **job_options,
            )

        from baldur.core.retry import RetryConfig, retry_with_backoff

        outcome = retry_with_backoff(
            _do_enqueue,
            RetryConfig(
                max_retries=3,
                backoff=ExponentialBackoff(base_delay=0.5, max_delay=5.0),
                retryable_exceptions=(
                    ConnectionError,
                    OSError,
                    TimeoutError,
                ),
                context_name="rq_enqueue",
            ),
        )

        if outcome.success:
            job = outcome.result
            assert job is not None  # success ⇒ job populated
            logger.info(
                "rq.enqueued_task",
                task_name=task_name,
                job=job.id,
            )
            return job.id
        raise outcome.exception  # type: ignore[misc]

    def _get_retry_intervals(self, options: TaskOptions) -> list[int]:
        """Calculate retry intervals with exponential backoff.

        Honors ``options.retry_jitter``: when set, each enqueue draws its own
        jittered interval list so storm retries desynchronize at the RQ server.
        The canonical strategy jitters *after* capping, so each interval is
        re-clamped to ``retry_backoff_max`` and rounded to whole seconds (RQ
        ``Retry(interval=...)`` takes int seconds).
        """
        if options.retry_backoff:
            backoff = ExponentialBackoff(
                base_delay=_RQ_RETRY_BASE_SECONDS,
                multiplier=_RQ_RETRY_MULTIPLIER,
                max_delay=options.retry_backoff_max,
                jitter=options.retry_jitter,
            )
            return [
                min(int(round(delay)), options.retry_backoff_max)
                for delay in backoff.delays(options.max_retries)
            ]
        # Fixed base-second intervals
        return [int(_RQ_RETRY_BASE_SECONDS)] * options.max_retries

    def enqueue_many(
        self,
        tasks: list[tuple[str, tuple, dict]],
        options: TaskOptions | None = None,
    ) -> list[str]:
        """
        Enqueue multiple tasks.

        Args:
            tasks: List of (task_name, args, kwargs) tuples
            options: Shared execution options

        Returns:
            List of task IDs
        """
        task_ids = []
        for task_name, args, kwargs in tasks:
            task_id = self.enqueue(task_name, args=args, kwargs=kwargs, options=options)
            task_ids.append(task_id)
        return task_ids

    # =========================================================================
    # Task Management
    # =========================================================================

    def get_result(
        self,
        task_id: str,
        timeout: float | None = None,
    ) -> TaskResult:
        """
        Get task result.

        Args:
            task_id: Task ID from enqueue
            timeout: Max seconds to wait for completion

        Returns:
            TaskResult with status and result/error
        """
        try:
            from rq.job import Job

            job = Job.fetch(task_id, connection=self.connection)

            # Map RQ status to TaskStatus
            status_map = {
                "queued": TaskStatus.PENDING,
                "started": TaskStatus.STARTED,
                "deferred": TaskStatus.PENDING,
                "finished": TaskStatus.SUCCESS,
                "stopped": TaskStatus.REVOKED,
                "scheduled": TaskStatus.PENDING,
                "failed": TaskStatus.FAILURE,
                "canceled": TaskStatus.REVOKED,
            }

            status = status_map.get(job.get_status(), TaskStatus.PENDING)

            return TaskResult(
                task_id=task_id,
                status=status,
                result=job.result if status == TaskStatus.SUCCESS else None,
                error=str(job.exc_info) if job.exc_info else None,
                retries=getattr(job, "retries_left", 0) or 0,
                started_at=job.started_at,
                completed_at=job.ended_at,
            )

        except Exception as e:
            logger.exception(
                "rq.get_result_failed",
                task_id=task_id,
                error=e,
            )
            return TaskResult(
                task_id=task_id,
                status=TaskStatus.FAILURE,
                error=str(e),
            )

    def revoke(
        self,
        task_id: str,
        terminate: bool = False,
        signal: str = "SIGTERM",
    ) -> bool:
        """
        Cancel a pending or running job.

        Args:
            task_id: Task ID to cancel
            terminate: If True, terminate running task
            signal: Not used in RQ

        Returns:
            True if task was revoked
        """
        try:
            from rq.job import Job

            job = Job.fetch(task_id, connection=self.connection)

            if terminate:
                job.cancel()
            else:
                job.cancel()

            logger.info(
                "rq.revoked_task",
                task_id=task_id,
            )
            return True

        except Exception as e:
            logger.exception(
                "rq.revoke_task_failed",
                task_id=task_id,
                error=e,
            )
            return False

    def retry(
        self,
        task_id: str,
        countdown: int | None = None,
        max_retries: int | None = None,
    ) -> str:
        """
        Retry a failed task.

        Args:
            task_id: Original task ID
            countdown: Delay before retry
            max_retries: Not used in this implementation

        Returns:
            New task ID
        """
        try:
            from rq.job import Job

            job = Job.fetch(task_id, connection=self.connection)
            job.requeue()

            logger.info(
                "rq.requeued_task",
                task_id=task_id,
            )
            return task_id

        except Exception as e:
            logger.exception(
                "rq.retry_task_failed",
                task_id=task_id,
                error=e,
            )
            raise

    # =========================================================================
    # Scheduling
    # =========================================================================

    def schedule_periodic(
        self,
        task_name: str,
        schedule: timedelta,
        args: tuple = (),
        kwargs: dict | None = None,
        name: str | None = None,
    ) -> str:
        """
        Schedule a periodic task using RQ-scheduler.

        Note: Requires rq-scheduler to be installed and running.

        Args:
            task_name: Registered task name
            schedule: Execution interval
            args: Positional arguments
            kwargs: Keyword arguments
            name: Unique schedule name

        Returns:
            Schedule ID
        """
        try:
            from rq_scheduler import Scheduler

            scheduler = Scheduler(connection=self.connection)
            task_info = self._tasks.get(task_name)

            if task_info is None:
                raise ValueError(f"Task not found: {task_name}")

            job = scheduler.schedule(
                scheduled_time=datetime.now(),
                func=task_info["func"],
                args=args,
                kwargs=kwargs or {},
                interval=int(schedule.total_seconds()),
                repeat=None,  # Repeat forever
            )

            logger.info(
                "rq.scheduled_periodic_task",
                task_name=task_name,
                job=job.id,
            )
            return job.id

        except ImportError as err:
            raise ImportError(
                "rq-scheduler is required for periodic tasks. "
                "Install it with: pip install rq-scheduler"
            ) from err

    def unschedule(self, schedule_id: str) -> bool:
        """
        Remove a periodic schedule.

        Args:
            schedule_id: Schedule ID from schedule_periodic

        Returns:
            True if schedule was removed
        """
        try:
            from rq_scheduler import Scheduler

            scheduler = Scheduler(connection=self.connection)
            scheduler.cancel(schedule_id)

            logger.info(
                "rq.unscheduled",
                schedule_id=schedule_id,
            )
            return True

        except Exception as e:
            logger.exception(
                "rq.unschedule_failed",
                schedule_id=schedule_id,
                error=e,
            )
            return False

    # =========================================================================
    # Queue Management
    # =========================================================================

    def purge_queue(self, queue_name: str = "default") -> int:
        """
        Remove all pending tasks from a queue.

        Returns:
            Number of tasks purged
        """
        try:
            queue = self._get_queue(queue_name)
            count = queue.count
            queue.empty()

            logger.info(
                "rq.purged_tasks_queue",
                purged_count=count,
                queue_name=queue_name,
            )
            return count

        except Exception as e:
            logger.exception(
                "rq.purge_queue_failed",
                queue_name=queue_name,
                error=e,
            )
            return 0

    def queue_length(self, queue_name: str = "default") -> int:
        """Get number of pending tasks in queue."""
        try:
            queue = self._get_queue(queue_name)
            return queue.count
        except Exception as e:
            logger.exception(
                "rq.get_queue_length_failed",
                queue_name=queue_name,
                error=e,
            )
            return 0

    # =========================================================================
    # Health Check
    # =========================================================================

    def health_check(self) -> bool:
        """Check if RQ/Redis is reachable."""
        try:
            self.connection.ping()
            return True
        except Exception as e:
            logger.exception(
                "rq.health_check_failed",
                error=e,
            )
            return False


class RQAsyncResult:
    """
    Celery-compatible async result wrapper for RQ jobs.

    Provides a consistent interface for checking task status and results.
    """

    def __init__(self, task_id: str, adapter: RQTaskAdapter):
        """
        Initialize async result.

        Args:
            task_id: RQ job ID
            adapter: RQTaskAdapter instance for fetching results
        """
        self.task_id = task_id
        self.id = task_id
        self._adapter = adapter

    def get(self, timeout: float | None = None) -> Any:
        """
        Get task result, blocking until complete.

        Args:
            timeout: Max seconds to wait

        Returns:
            Task result

        Raises:
            Exception: If task failed
        """
        import time

        start = time.time()
        while True:
            result = self._adapter.get_result(self.task_id)

            if result.status == TaskStatus.SUCCESS:
                return result.result
            if result.status == TaskStatus.FAILURE:
                raise RuntimeError(result.error)
            if result.status == TaskStatus.REVOKED:
                raise RuntimeError("Task was revoked")

            if timeout and (time.time() - start) > timeout:
                raise TimeoutError(f"Task {self.task_id} did not complete in time")

            time.sleep(0.1)

    def ready(self) -> bool:
        """Check if task has completed."""
        result = self._adapter.get_result(self.task_id)
        return result.status in (
            TaskStatus.SUCCESS,
            TaskStatus.FAILURE,
            TaskStatus.REVOKED,
        )

    def successful(self) -> bool:
        """Check if task completed successfully."""
        result = self._adapter.get_result(self.task_id)
        return result.status == TaskStatus.SUCCESS

    def failed(self) -> bool:
        """Check if task failed."""
        result = self._adapter.get_result(self.task_id)
        return result.status == TaskStatus.FAILURE

    @property
    def status(self) -> str:
        """Get current task status."""
        result = self._adapter.get_result(self.task_id)
        return result.status.value

    @property
    def result(self) -> Any:
        """Get task result (non-blocking)."""
        result = self._adapter.get_result(self.task_id)
        return result.result
