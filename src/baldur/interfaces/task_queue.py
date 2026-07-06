"""
Task Queue Interface for Baldur System

Abstract interface for async task execution and scheduling.
Enables switching between task queue backends (Celery, RQ, Dramatiq).

Design Principles:
1. Pure Python - no framework dependencies
2. Dataclasses for immutable DTOs
3. ABC for queue contracts
4. Support for both immediate and scheduled execution
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum, IntEnum
from typing import TYPE_CHECKING, Any, ParamSpec, TypeVar

from baldur.core.exceptions import BaldurError

if TYPE_CHECKING:
    from collections.abc import Awaitable

F = TypeVar("F", bound=Callable)
P = ParamSpec("P")
R = TypeVar("R")

__all__ = [
    # Enums
    "TaskStatus",
    "TaskPriority",
    # DTOs
    "TaskResult",
    "TaskOptions",
    "ScheduleInfo",
    # Exceptions
    "TaskQueueError",
    "TaskNotFoundError",
    "TaskTimeoutError",
    "TaskRevokedError",
    "PartialEnqueueError",
    # Interfaces
    "TaskQueueInterface",
    "AsyncTaskQueueInterface",
]


# ============================================================================
# Enums
# ============================================================================


class TaskStatus(str, Enum):
    """Task execution status"""

    PENDING = "pending"  # Task queued, not yet started
    STARTED = "started"  # Task execution began
    SUCCESS = "success"  # Task completed successfully
    FAILURE = "failure"  # Task failed after all retries
    RETRY = "retry"  # Task is being retried
    REVOKED = "revoked"  # Task was cancelled


class TaskPriority(IntEnum):
    """Task priority levels (higher = processed sooner)"""

    LOW = 0
    NORMAL = 5
    HIGH = 10
    CRITICAL = 20


# ============================================================================
# Data Transfer Objects (DTOs)
# ============================================================================


@dataclass(frozen=True)
class TaskResult:
    """
    Result of task execution or status check.

    Immutable dataclass representing the outcome or current
    state of a queued task.

    Attributes:
        task_id: Unique task identifier
        status: Current task status
        result: Return value if task succeeded
        error: Error message if task failed
        traceback: Full traceback string if task failed
        retries: Number of retry attempts made
        started_at: When task execution began
        completed_at: When task execution completed
    """

    task_id: str
    status: TaskStatus
    result: Any | None = None
    error: str | None = None
    traceback: str | None = None
    retries: int = 0
    started_at: datetime | None = None
    completed_at: datetime | None = None

    @property
    def is_finished(self) -> bool:
        """Check if task has completed (success or failure)."""
        return self.status in (
            TaskStatus.SUCCESS,
            TaskStatus.FAILURE,
            TaskStatus.REVOKED,
        )

    @property
    def is_successful(self) -> bool:
        """Check if task completed successfully."""
        return self.status == TaskStatus.SUCCESS

    @property
    def duration(self) -> timedelta | None:
        """Calculate task execution duration."""
        if self.started_at and self.completed_at:
            return self.completed_at - self.started_at
        return None


@dataclass
class TaskOptions:
    """
    Options for task enqueueing.

    Configures how a task should be executed, including
    scheduling, retries, and queue selection.

    Attributes:
        countdown: Delay in seconds before execution
        eta: Exact time to execute task
        expires: Task expiration time (won't run after this)
        retry: Enable automatic retries on failure
        max_retries: Maximum retry attempts
        retry_backoff: Use exponential backoff between retries
        retry_backoff_max: Maximum backoff delay in seconds
        retry_jitter: Add randomness to backoff delays
        queue: Target queue name (default uses 'default')
        priority: Task priority (higher = processed sooner)
        timeout: Task execution timeout in seconds
        soft_timeout: Soft timeout (raises SoftTimeLimitExceeded)
    """

    countdown: int | None = None
    eta: datetime | None = None
    expires: datetime | None = None
    retry: bool = True
    max_retries: int = 3
    retry_backoff: bool = True
    retry_backoff_max: int = 600
    retry_jitter: bool = True
    queue: str | None = None
    priority: TaskPriority = TaskPriority.NORMAL
    timeout: int | None = None
    soft_timeout: int | None = None

    def with_countdown(self, seconds: int) -> TaskOptions:
        """Create new options with countdown."""
        return TaskOptions(
            countdown=seconds,
            eta=self.eta,
            expires=self.expires,
            retry=self.retry,
            max_retries=self.max_retries,
            retry_backoff=self.retry_backoff,
            retry_backoff_max=self.retry_backoff_max,
            retry_jitter=self.retry_jitter,
            queue=self.queue,
            priority=self.priority,
            timeout=self.timeout,
            soft_timeout=self.soft_timeout,
        )

    def with_priority(self, priority: TaskPriority) -> TaskOptions:
        """Create new options with priority."""
        return TaskOptions(
            countdown=self.countdown,
            eta=self.eta,
            expires=self.expires,
            retry=self.retry,
            max_retries=self.max_retries,
            retry_backoff=self.retry_backoff,
            retry_backoff_max=self.retry_backoff_max,
            retry_jitter=self.retry_jitter,
            queue=self.queue,
            priority=priority,
            timeout=self.timeout,
            soft_timeout=self.soft_timeout,
        )


@dataclass(frozen=True)
class ScheduleInfo:
    """
    Information about a periodic schedule.

    Attributes:
        schedule_id: Unique schedule identifier
        task_name: Name of the scheduled task
        interval: Execution interval
        args: Positional arguments for task
        kwargs: Keyword arguments for task
        last_run: When task last executed
        next_run: When task will next execute
        enabled: Whether schedule is active
    """

    schedule_id: str
    task_name: str
    interval: timedelta
    args: tuple = field(default_factory=tuple)
    kwargs: dict = field(default_factory=dict)
    last_run: datetime | None = None
    next_run: datetime | None = None
    enabled: bool = True


# ============================================================================
# Exceptions
# ============================================================================


class TaskQueueError(BaldurError):
    """Base exception for task queue errors."""

    pass


class TaskNotFoundError(TaskQueueError):
    """Raised when a task is not registered."""

    pass


class TaskTimeoutError(TaskQueueError):
    """Raised when task execution times out."""

    pass


class TaskRevokedError(TaskQueueError):
    """Raised when a revoked task is accessed."""

    pass


class PartialEnqueueError(TaskQueueError):
    """Raised when some tasks in a batch fail to enqueue.

    Attributes:
        succeeded: List of (original_index, task_id) tuples for
            tasks that were successfully enqueued.
        failed: List of (original_index, exception) tuples for
            tasks that failed to enqueue.
    """

    def __init__(
        self,
        succeeded: list[tuple[int, str]],
        failed: list[tuple[int, Exception]],
    ) -> None:
        self.succeeded = succeeded
        self.failed = failed
        super().__init__(
            f"Partial enqueue: {len(succeeded)} succeeded, {len(failed)} failed"
        )


# ============================================================================
# Task Queue Interface
# ============================================================================


class TaskQueueInterface(ABC):
    """
    Abstract interface for async task queues.

    This interface defines the contract for background task
    execution systems. It enables the baldur system to
    work with different task queue backends interchangeably.

    Implementations:
        - CeleryTaskAdapter (current - Celery)
        - RQTaskAdapter (planned - Redis Queue)
        - DramatiqTaskAdapter (planned)
        - SyncTaskAdapter (for testing - synchronous execution)

    Example:
        >>> queue = ProviderRegistry.get_queue()
        >>>
        >>> # Enqueue a task
        >>> task_id = queue.enqueue(
        ...     "process_payment",
        ...     args=(order_id,),
        ...     options=TaskOptions(priority=TaskPriority.HIGH),
        ... )
        >>>
        >>> # Check result later
        >>> result = queue.get_result(task_id)
        >>> if result.is_successful:
        ...     print(f"Payment processed: {result.result}")
    """

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """
        Return the provider name.

        Returns:
            Provider identifier (e.g., 'celery', 'rq', 'dramatiq')
        """
        pass

    # =========================================================================
    # Task Registration
    # =========================================================================

    @abstractmethod
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
        Decorator to register a function as a task.

        Args:
            name: Task name (default: function qualified name)
            bind: If True, pass task instance as first argument
            max_retries: Maximum retry attempts on failure
            autoretry_for: Exception types to automatically retry
            retry_backoff: Use exponential backoff between retries
            retry_backoff_max: Maximum backoff delay in seconds
            retry_jitter: Add randomness to prevent thundering herd
            rate_limit: Rate limit (e.g., "10/m" for 10 per minute)
            time_limit: Hard time limit in seconds
            soft_time_limit: Soft time limit (raises exception)

        Returns:
            Decorator function

        Example:
            >>> @queue.task(max_retries=5, autoretry_for=(ConnectionError,))
            ... def process_payment(payment_id: int):
            ...     # Process the payment
            ...     pass
        """
        pass

    def register_task(
        self,
        func: Callable,
        name: str | None = None,
        **options: Any,
    ) -> str:
        """
        Register a function as a task programmatically.

        Args:
            func: Function to register
            name: Task name (default: function qualified name)
            **options: Additional task options

        Returns:
            Registered task name
        """
        decorator = self.task(name=name, **options)
        decorator(func)
        return name or f"{func.__module__}.{func.__qualname__}"

    # =========================================================================
    # Task Execution
    # =========================================================================

    @abstractmethod
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
            args: Positional arguments for task
            kwargs: Keyword arguments for task
            options: Execution options

        Returns:
            Task ID for tracking

        Raises:
            TaskNotFoundError: If task_name is not registered

        Example:
            >>> task_id = queue.enqueue(
            ...     "send_notification",
            ...     args=(user_id, "Welcome!"),
            ...     options=TaskOptions(countdown=60),
            ... )
        """
        pass

    @abstractmethod
    def enqueue_many(
        self,
        tasks: list[tuple[str, tuple, dict]],
        options: TaskOptions | None = None,
    ) -> list[str]:
        """
        Enqueue multiple tasks atomically.

        Args:
            tasks: List of (task_name, args, kwargs) tuples
            options: Shared execution options for all tasks

        Returns:
            List of task IDs in same order as input

        Note:
            Implementations should ensure either all tasks are
            enqueued or none are (atomic operation).
        """
        pass

    def delay(
        self,
        task_name: str,
        *args: Any,
        **kwargs: Any,
    ) -> str:
        """
        Convenience method to enqueue a task immediately.

        Args:
            task_name: Registered task name
            *args: Positional arguments for task
            **kwargs: Keyword arguments for task

        Returns:
            Task ID for tracking
        """
        return self.enqueue(task_name, args=args, kwargs=kwargs)

    def apply_async(
        self,
        task_name: str,
        args: tuple = (),
        kwargs: dict | None = None,
        countdown: int | None = None,
        eta: datetime | None = None,
        **extra_options: Any,
    ) -> str:
        """
        Enqueue a task with common options as keyword arguments.

        Args:
            task_name: Registered task name
            args: Positional arguments
            kwargs: Keyword arguments
            countdown: Delay in seconds
            eta: Exact execution time
            **extra_options: Additional TaskOptions fields

        Returns:
            Task ID for tracking
        """
        options = TaskOptions(
            countdown=countdown,
            eta=eta,
            **extra_options,
        )
        return self.enqueue(task_name, args=args, kwargs=kwargs, options=options)

    # =========================================================================
    # Task Management
    # =========================================================================

    @abstractmethod
    def get_result(
        self,
        task_id: str,
        timeout: float | None = None,
    ) -> TaskResult:
        """
        Get task result (may block if timeout provided).

        Args:
            task_id: Task ID from enqueue
            timeout: Max seconds to wait for completion

        Returns:
            TaskResult with status and result/error

        Note:
            If timeout is None, returns immediately with current status.
            If timeout is provided, blocks until task completes or
            timeout expires.
        """
        pass

    @abstractmethod
    def revoke(
        self,
        task_id: str,
        terminate: bool = False,
        signal: str = "SIGTERM",
    ) -> bool:
        """
        Cancel a pending or running task.

        Args:
            task_id: Task ID to cancel
            terminate: If True, terminate running task
            signal: Signal to send if terminating

        Returns:
            True if task was revoked

        Note:
            Revoking a pending task prevents execution.
            Terminating a running task sends the specified signal.
        """
        pass

    @abstractmethod
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
            max_retries: Override maximum retries

        Returns:
            New task ID for the retry

        Note:
            This creates a new task based on the original.
            The original task's state remains unchanged.
        """
        pass

    def forget(self, task_id: str) -> bool:
        """
        Forget a task result (cleanup).

        Args:
            task_id: Task ID to forget

        Returns:
            True if result was forgotten
        """
        return True

    # =========================================================================
    # Scheduling (Periodic Tasks)
    # =========================================================================

    @abstractmethod
    def schedule_periodic(
        self,
        task_name: str,
        schedule: timedelta,
        args: tuple = (),
        kwargs: dict | None = None,
        name: str | None = None,
    ) -> str:
        """
        Schedule a periodic task.

        Args:
            task_name: Registered task name
            schedule: Execution interval
            args: Positional arguments for task
            kwargs: Keyword arguments for task
            name: Unique schedule name (auto-generated if not provided)

        Returns:
            Schedule ID

        Example:
            >>> schedule_id = queue.schedule_periodic(
            ...     "cleanup_expired_tokens",
            ...     schedule=timedelta(hours=1),
            ... )
        """
        pass

    @abstractmethod
    def unschedule(self, schedule_id: str) -> bool:
        """
        Remove a periodic schedule.

        Args:
            schedule_id: Schedule ID to remove

        Returns:
            True if schedule was removed
        """
        pass

    def get_schedule(self, schedule_id: str) -> ScheduleInfo | None:
        """
        Get information about a periodic schedule.

        Args:
            schedule_id: Schedule ID to query

        Returns:
            ScheduleInfo or None if not found
        """
        return None

    def list_schedules(self) -> list[ScheduleInfo]:
        """
        List all periodic schedules.

        Returns:
            List of ScheduleInfo objects
        """
        return []

    # =========================================================================
    # Queue Management
    # =========================================================================

    @abstractmethod
    def purge_queue(self, queue_name: str = "default") -> int:
        """
        Remove all pending tasks from a queue.

        Args:
            queue_name: Queue to purge

        Returns:
            Number of tasks purged

        Warning:
            This permanently removes all pending tasks.
            Use with caution in production.
        """
        pass

    @abstractmethod
    def queue_length(self, queue_name: str = "default") -> int:
        """
        Get number of pending tasks in queue.

        Args:
            queue_name: Queue to check

        Returns:
            Number of pending tasks
        """
        pass

    def list_queues(self) -> list[str]:
        """
        List all known queue names.

        Returns:
            List of queue names
        """
        return ["default"]

    def active_count(self) -> int:
        """
        Get number of currently executing tasks.

        Returns:
            Number of active tasks across all workers
        """
        return 0

    # =========================================================================
    # Health Check
    # =========================================================================

    @abstractmethod
    def health_check(self) -> bool:
        """
        Check if task queue backend is reachable.

        Returns:
            True if broker and backend are healthy
        """
        pass

    def worker_count(self) -> int:
        """
        Get number of active workers.

        Returns:
            Number of workers processing tasks
        """
        return 0

    def ping(self) -> bool:
        """
        Simple connectivity check.

        Returns:
            True if connection is alive
        """
        return self.health_check()


# ============================================================================
# Async Task Queue Interface
# ============================================================================


class AsyncTaskQueueInterface(ABC):
    """
    Async interface for task queues.

    For async-native backends (arq, Taskiq, SAQ).
    Sync backends (Celery, RQ) use TaskQueueInterface instead.

    Implementations:
        - ArqTaskAdapter (arq - Redis-based async)
        - AsyncSyncTaskAdapter (for testing - async wrapper over sync)
    """

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Return the provider name (e.g., 'arq', 'taskiq')."""
        ...

    # =========================================================================
    # Task Registration
    # =========================================================================

    @abstractmethod
    def task(
        self,
        name: str | None = None,
        *,
        max_retries: int = 3,
        timeout: int | None = None,
        queue: str | None = None,
    ) -> Callable[[Callable[P, Awaitable[R]]], Callable[P, Awaitable[R]]]:
        """
        Decorator to register an async function as a task.

        Registered functions MUST be async (coroutines).
        Type signature enforces this at type-check time —
        mypy will reject sync function registration.
        """
        ...

    # =========================================================================
    # Task Execution
    # =========================================================================

    @abstractmethod
    async def enqueue(
        self,
        task_name: str,
        args: tuple = (),
        kwargs: dict | None = None,
        options: TaskOptions | None = None,
    ) -> str:
        """Enqueue a task for async execution. Returns task ID."""
        ...

    @abstractmethod
    async def enqueue_many(
        self,
        tasks: list[tuple[str, tuple, dict]],
        options: TaskOptions | None = None,
    ) -> list[str]:
        """Enqueue multiple tasks. Returns list of task IDs.

        Raises:
            PartialEnqueueError: Implementations may raise this when
                some tasks fail. Contains succeeded (index, ID) pairs
                and failed (index, exception) pairs for caller-side
                recovery.
        """
        ...

    async def delay(self, task_name: str, *args: Any, **kwargs: Any) -> str:
        """Convenience: enqueue immediately with positional/keyword args."""
        return await self.enqueue(task_name, args=args, kwargs=kwargs)

    # =========================================================================
    # Task Management
    # =========================================================================

    @abstractmethod
    async def get_result(
        self,
        task_id: str,
        timeout: float | None = None,
    ) -> TaskResult:
        """Get task result. Non-blocking if timeout is None."""
        ...

    @abstractmethod
    async def revoke(self, task_id: str) -> bool:
        """Cancel a pending task. Returns True if revoked."""
        ...

    # =========================================================================
    # Queue Management
    # =========================================================================

    @abstractmethod
    async def queue_length(self, queue_name: str = "default") -> int:
        """Get number of pending tasks in queue."""
        ...

    @abstractmethod
    async def health_check(self) -> bool:
        """Check if task queue backend is reachable."""
        ...

    # =========================================================================
    # Scheduling (optional)
    # =========================================================================

    async def schedule_periodic(
        self,
        task_name: str,
        cron: str | None = None,
        interval: timedelta | None = None,
        args: tuple = (),
        kwargs: dict | None = None,
    ) -> str:
        """
        Schedule a periodic task.

        Not all async backends support this natively.
        arq supports cron-based scheduling; others may require
        external schedulers (APScheduler, K8s CronJob).

        Args:
            cron: Cron expression (e.g., "*/5 * * * *")
            interval: Alternative to cron — fixed interval
        """
        raise NotImplementedError(
            f"{self.provider_name} does not support periodic scheduling. "
            "Use APScheduler or K8s CronJob instead."
        )

    async def unschedule(self, schedule_id: str) -> bool:
        """Remove a periodic schedule."""
        raise NotImplementedError(
            f"{self.provider_name} does not support periodic scheduling."
        )

    # =========================================================================
    # Lifecycle
    # =========================================================================

    async def startup(self) -> None:  # noqa: B027
        """Initialize connections (called once at app startup)."""

    async def shutdown(self) -> None:  # noqa: B027
        """Close connections (called once at app shutdown)."""
