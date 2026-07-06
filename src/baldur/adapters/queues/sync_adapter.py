"""
Synchronous Task Queue Adapter for Baldur System

Synchronous implementation of TaskQueueInterface for testing.
Executes tasks immediately in the same process.

Warning:
    This adapter is for TESTING ONLY. Tasks are executed
    synchronously and there is no distributed processing.

Features:
    - Immediate task execution
    - Full result tracking
    - Configurable failure injection
    - No external dependencies
"""

from __future__ import annotations

import traceback
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from functools import wraps
from typing import Any, TypeVar

import structlog

from baldur.interfaces.task_queue import (
    ScheduleInfo,
    TaskNotFoundError,
    TaskOptions,
    TaskQueueInterface,
    TaskResult,
    TaskStatus,
)

logger = structlog.get_logger()

F = TypeVar("F", bound=Callable)


@dataclass
class TaskRecord:
    """Internal record of a task execution."""

    task_id: str
    task_name: str
    args: tuple
    kwargs: dict
    status: TaskStatus
    result: Any = None
    error: str | None = None
    traceback: str | None = None
    retries: int = 0
    created_at: datetime = field(default_factory=datetime.now)
    started_at: datetime | None = None
    completed_at: datetime | None = None


@dataclass
class RegisteredTask:
    """Metadata for a registered task."""

    name: str
    func: Callable
    bind: bool = False
    max_retries: int = 3
    autoretry_for: tuple[type[Exception], ...] = ()


class SyncTaskAdapter(TaskQueueInterface):
    """
    Synchronous implementation of TaskQueueInterface for testing.

    Tasks are executed immediately when enqueued, making this
    adapter ideal for unit tests and development.

    Features:
        - Immediate synchronous execution
        - Complete result tracking
        - Failure injection for testing retries
        - No external dependencies required

    Example:
        >>> queue = SyncTaskAdapter()
        >>>
        >>> @queue.task(name="process_payment")
        >>> def process_payment(payment_id: int) -> str:
        ...     return f"Processed {payment_id}"
        >>>
        >>> task_id = queue.enqueue("process_payment", args=(123,))
        >>> result = queue.get_result(task_id)
        >>> assert result.is_successful
        >>> assert result.result == "Processed 123"

    Warning:
        This is for TESTING ONLY. Not suitable for production.
    """

    def __init__(self) -> None:
        """Initialize synchronous task adapter."""
        self._tasks: dict[str, RegisteredTask] = {}
        self._results: dict[str, TaskRecord] = {}
        self._schedules: dict[str, ScheduleInfo] = {}
        self._pending_queue: list[str] = []

        # Testing controls
        self._should_fail_next: bool = False
        self._fail_error: str = "Injected failure"
        self._delay_execution: bool = False
        self._healthy: bool = True

    @property
    def provider_name(self) -> str:
        """Return 'sync' as the provider identifier."""
        return "sync"

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
        Decorator to register a function as a task.

        In sync mode, most options are ignored but stored for compatibility.
        """

        def decorator(func: F) -> F:
            task_name = name or f"{func.__module__}.{func.__qualname__}"

            # Register the task
            self._tasks[task_name] = RegisteredTask(
                name=task_name,
                func=func,
                bind=bind,
                max_retries=max_retries,
                autoretry_for=autoretry_for,
            )

            logger.debug(
                "cell_registry.bulkheads_registered",
                task_name=task_name,
            )

            # Return wrapper that allows .delay() calls. Typed as Any so the
            # Celery-compatibility attributes (delay/apply_async/name) can be
            # attached without per-attr type-ignore.
            @wraps(func)
            def wrapper_fn(*args: Any, **kwargs: Any) -> Any:
                return func(*args, **kwargs)

            wrapper: Any = wrapper_fn

            # Add delay method for Celery compatibility
            wrapper.delay = lambda *a, **kw: self.enqueue(task_name, args=a, kwargs=kw)
            wrapper.apply_async = lambda args=(), kwargs=None, **opts: self.enqueue(
                task_name, args=args, kwargs=kwargs or {}
            )
            wrapper.name = task_name

            return wrapper  # type: ignore[no-any-return]

        return decorator

    def _get_task(self, task_name: str) -> RegisteredTask:
        """Get a registered task by name."""
        if task_name not in self._tasks:
            raise TaskNotFoundError(f"Task not found: {task_name}")
        return self._tasks[task_name]

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
        Enqueue and immediately execute a task.

        In sync mode, tasks are executed synchronously.
        """
        registered = self._get_task(task_name)
        kwargs = kwargs or {}
        options = options or TaskOptions()

        # Generate task ID
        task_id = str(uuid.uuid4())

        # Create task record
        record = TaskRecord(
            task_id=task_id,
            task_name=task_name,
            args=args,
            kwargs=kwargs,
            status=TaskStatus.PENDING,
        )

        logger.debug(
            "sync_adapter.executing_task",
            task_name=task_name,
            task_id=task_id,
        )

        # Handle delayed execution (just mark as pending, don't execute)
        if (
            self._delay_execution
            or options.countdown is not None
            or options.eta is not None
        ):
            self._results[task_id] = record
            self._pending_queue.append(task_id)
            return task_id

        # Execute immediately
        self._execute_task(record, registered)
        self._results[task_id] = record

        return task_id

    def _execute_task(
        self,
        record: TaskRecord,
        registered: RegisteredTask,
    ) -> None:
        """Execute a task and update the record."""
        record.status = TaskStatus.STARTED
        record.started_at = datetime.now()

        # Check for injected failure
        if self._should_fail_next:
            self._should_fail_next = False
            record.status = TaskStatus.FAILURE
            record.error = self._fail_error
            record.completed_at = datetime.now()
            return

        try:
            # Execute the task
            result = registered.func(*record.args, **record.kwargs)

            record.status = TaskStatus.SUCCESS
            record.result = result
            record.completed_at = datetime.now()

            logger.debug(
                "sync_adapter.task_succeeded",
                task_id=record.task_id,
            )

        except registered.autoretry_for as e:
            # Auto-retry for configured exceptions
            if record.retries < registered.max_retries:
                record.retries += 1
                record.status = TaskStatus.RETRY
                logger.debug(
                    "sync_adapter.retrying_task_attempt",
                    task_id=record.task_id,
                    retries=record.retries,
                )
                self._execute_task(record, registered)
            else:
                record.status = TaskStatus.FAILURE
                record.error = str(e)
                record.traceback = traceback.format_exc()
                record.completed_at = datetime.now()

        except Exception as e:
            record.status = TaskStatus.FAILURE
            record.error = str(e)
            record.traceback = traceback.format_exc()
            record.completed_at = datetime.now()

            logger.exception(
                "sync_adapter.task_failed",
                task_id=record.task_id,
                error=e,
            )

    def enqueue_many(
        self,
        tasks: list[tuple[str, tuple, dict]],
        options: TaskOptions | None = None,
    ) -> list[str]:
        """Enqueue and execute multiple tasks."""
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

        Since tasks execute synchronously, this always returns immediately.
        """
        if task_id not in self._results:
            return TaskResult(
                task_id=task_id,
                status=TaskStatus.PENDING,
            )

        record = self._results[task_id]

        return TaskResult(
            task_id=task_id,
            status=record.status,
            result=record.result,
            error=record.error,
            traceback=record.traceback,
            retries=record.retries,
            started_at=record.started_at,
            completed_at=record.completed_at,
        )

    def revoke(
        self,
        task_id: str,
        terminate: bool = False,
        signal: str = "SIGTERM",
    ) -> bool:
        """
        Revoke a pending task.

        In sync mode, only pending (delayed) tasks can be revoked.
        """
        if task_id in self._pending_queue:
            self._pending_queue.remove(task_id)
            if task_id in self._results:
                self._results[task_id].status = TaskStatus.REVOKED
            logger.debug(
                "sync_adapter.revoked_task",
                task_id=task_id,
            )
            return True
        return False

    def retry(
        self,
        task_id: str,
        countdown: int | None = None,
        max_retries: int | None = None,
    ) -> str:
        """
        Retry a task by re-executing it.

        Creates a new task with the same arguments.
        """
        if task_id not in self._results:
            raise TaskNotFoundError(f"Task not found: {task_id}")

        record = self._results[task_id]
        options = TaskOptions(countdown=countdown)

        return self.enqueue(
            record.task_name,
            args=record.args,
            kwargs=record.kwargs,
            options=options,
        )

    def forget(self, task_id: str) -> bool:
        """Remove a task result from memory."""
        if task_id in self._results:
            del self._results[task_id]
            return True
        return False

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
        Register a periodic task.

        Note: In sync mode, periodic tasks are just registered but not
        automatically executed. Call run_scheduled() to execute them.
        """
        schedule_id = name or f"schedule_{task_name}_{id(schedule)}"

        self._schedules[schedule_id] = ScheduleInfo(
            schedule_id=schedule_id,
            task_name=task_name,
            interval=schedule,
            args=args,
            kwargs=kwargs or {},
            enabled=True,
        )

        logger.debug(
            "sync_adapter.scheduled_periodic_task",
            schedule_id=schedule_id,
        )
        return schedule_id

    def unschedule(self, schedule_id: str) -> bool:
        """Remove a periodic schedule."""
        if schedule_id in self._schedules:
            del self._schedules[schedule_id]
            return True
        return False

    def get_schedule(self, schedule_id: str) -> ScheduleInfo | None:
        """Get information about a periodic schedule."""
        return self._schedules.get(schedule_id)

    def list_schedules(self) -> list[ScheduleInfo]:
        """List all periodic schedules."""
        return list(self._schedules.values())

    def run_scheduled(self) -> list[str]:
        """
        Execute all scheduled tasks once (for testing).

        Returns list of task IDs that were executed.
        """
        task_ids = []
        for schedule in self._schedules.values():
            if schedule.enabled:
                task_id = self.enqueue(
                    schedule.task_name,
                    args=schedule.args,
                    kwargs=dict(schedule.kwargs),
                )
                task_ids.append(task_id)
        return task_ids

    # =========================================================================
    # Queue Management
    # =========================================================================

    def purge_queue(self, queue_name: str = "default") -> int:
        """Clear pending queue."""
        count = len(self._pending_queue)
        self._pending_queue.clear()
        return count

    def queue_length(self, queue_name: str = "default") -> int:
        """Get number of pending tasks."""
        return len(self._pending_queue)

    def active_count(self) -> int:
        """In sync mode, no tasks are ever 'active' (they complete immediately)."""
        return 0

    # =========================================================================
    # Health Check
    # =========================================================================

    def health_check(self) -> bool:
        """Check if adapter is healthy."""
        return self._healthy

    def set_health_status(self, healthy: bool) -> None:
        """Set health status for testing."""
        self._healthy = healthy

    def worker_count(self) -> int:
        """In sync mode, there is always 1 'worker' (the main thread)."""
        return 1 if self._healthy else 0

    # =========================================================================
    # Testing Utilities
    # =========================================================================

    def fail_next(self, error_message: str = "Injected failure") -> SyncTaskAdapter:
        """
        Make the next task execution fail.

        Useful for testing error handling and retries.

        Example:
            >>> queue.fail_next("Database connection failed")
            >>> task_id = queue.enqueue("my_task", args=(1,))
            >>> result = queue.get_result(task_id)
            >>> assert result.status == TaskStatus.FAILURE
        """
        self._should_fail_next = True
        self._fail_error = error_message
        return self

    def enable_delayed_execution(self) -> SyncTaskAdapter:
        """
        Enable delayed execution mode.

        Tasks will be queued but not executed until run_pending() is called.
        """
        self._delay_execution = True
        return self

    def disable_delayed_execution(self) -> SyncTaskAdapter:
        """Disable delayed execution mode."""
        self._delay_execution = False
        return self

    def run_pending(self) -> list[str]:
        """
        Execute all pending tasks.

        Returns list of task IDs that were executed.
        """
        executed = []
        while self._pending_queue:
            task_id = self._pending_queue.pop(0)
            record = self._results[task_id]
            registered = self._get_task(record.task_name)
            self._execute_task(record, registered)
            executed.append(task_id)
        return executed

    def reset(self) -> SyncTaskAdapter:
        """Reset all state (for test cleanup)."""
        self._results.clear()
        self._pending_queue.clear()
        self._schedules.clear()
        self._should_fail_next = False
        self._delay_execution = False
        self._healthy = True
        # Keep task registrations
        return self

    def clear_all(self) -> SyncTaskAdapter:
        """Clear everything including task registrations."""
        self._tasks.clear()
        return self.reset()

    def get_all_results(self) -> dict[str, TaskResult]:
        """Get all task results (for testing inspection)."""
        return {task_id: self.get_result(task_id) for task_id in self._results}

    def get_call_count(self, task_name: str) -> int:
        """Get number of times a task was called."""
        return sum(
            1 for record in self._results.values() if record.task_name == task_name
        )
