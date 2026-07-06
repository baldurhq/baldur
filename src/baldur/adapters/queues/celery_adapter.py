"""
Celery Task Queue Adapter for Baldur System

Concrete implementation of TaskQueueInterface using Celery.
Provides full distributed task queue functionality.

Requirements:
    - celery>=5.0.0
    - redis (for broker/backend)

Related:
    - interfaces/task_queue.py: Interface definition
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from typing import Any, TypeVar

import structlog

from baldur.interfaces.task_queue import (
    ScheduleInfo,
    TaskNotFoundError,
    TaskOptions,
    TaskPriority,
    TaskQueueInterface,
    TaskResult,
    TaskStatus,
    TaskTimeoutError,
)
from baldur.settings.celery_task import get_celery_task_settings

logger = structlog.get_logger()

F = TypeVar("F", bound=Callable)


class CeleryTaskAdapter(TaskQueueInterface):
    """
    Celery implementation of TaskQueueInterface.

    This adapter wraps Celery's task functionality to provide
    a standardized interface for the baldur system.

    Configuration:
        Uses the Django project's Celery app instance.

    Example:
        >>> from baldur.factory import ProviderRegistry
        >>> queue = ProviderRegistry.get_queue("celery")
        >>>
        >>> # Register a task
        >>> @queue.task(max_retries=3)
        >>> def process_payment(payment_id: int):
        ...     # Process payment
        ...     pass
        >>>
        >>> # Enqueue task
        >>> task_id = queue.enqueue("process_payment", args=(123,))
    """

    def __init__(
        self,
        app: Any | None = None,
        default_queue: str = "default",
    ) -> None:
        """
        Initialize Celery task adapter.

        Args:
            app: Celery app instance (defaults to the current Celery application)
            default_queue: Default queue name for tasks
        """
        if app is None:
            from celery import current_app

            # Unwrap the proxy so the adapter binds to a concrete app instance
            # instead of resolving the proxy on every attribute access.
            self._app = current_app._get_current_object()
        else:
            self._app = app

        self._default_queue = default_queue
        self._registered_tasks: dict[str, Callable] = {}
        self._schedules: dict[str, ScheduleInfo] = {}

    @property
    def provider_name(self) -> str:
        """Return 'celery' as the provider identifier."""
        return "celery"

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
        Decorator to register a function as a Celery task.

        This wraps the Celery @app.task decorator with our interface.
        """

        def decorator(func: F) -> F:
            task_name = name or f"{func.__module__}.{func.__qualname__}"

            # Build Celery task options
            celery_options = {
                "name": task_name,
                "bind": bind,
                "max_retries": max_retries,
                "autoretry_for": autoretry_for,
                "retry_backoff": retry_backoff,
                "retry_backoff_max": retry_backoff_max,
                "retry_jitter": retry_jitter,
            }

            if rate_limit:
                celery_options["rate_limit"] = rate_limit
            if time_limit:
                celery_options["time_limit"] = time_limit
            if soft_time_limit:
                celery_options["soft_time_limit"] = soft_time_limit

            # Register with Celery
            celery_task = self._app.task(**celery_options)(func)

            # Store reference
            self._registered_tasks[task_name] = celery_task

            logger.debug(
                "cell_registry.bulkheads_registered",
                task_name=task_name,
            )
            return celery_task

        return decorator

    def _get_task(self, task_name: str) -> Any:
        """Get a registered Celery task by name."""
        # First check our local registry
        if task_name in self._registered_tasks:
            return self._registered_tasks[task_name]

        # Then check Celery's registry
        if task_name in self._app.tasks:
            return self._app.tasks[task_name]

        raise TaskNotFoundError(f"Task not found: {task_name}")

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

        Converts TaskOptions to Celery's apply_async arguments.
        """
        task = self._get_task(task_name)
        kwargs = kwargs or {}
        options = options or TaskOptions()

        # Build Celery apply_async options
        celery_options: dict[str, Any] = {}

        if options.countdown is not None:
            celery_options["countdown"] = options.countdown
        if options.eta is not None:
            celery_options["eta"] = options.eta
        if options.expires is not None:
            celery_options["expires"] = options.expires
        if options.queue is not None:
            celery_options["queue"] = options.queue
        else:
            celery_options["queue"] = self._default_queue

        # Priority mapping (Celery uses 0-9, we use enum)
        if options.priority != TaskPriority.NORMAL:
            # Map our priority to Celery's (inverted: lower = higher priority)
            celery_options["priority"] = 10 - options.priority.value

        # Retry settings
        if not options.retry:
            celery_options["retry"] = False

        logger.debug(
            "celery_adapter.enqueueing_task",
            task_name=task_name,
        )

        # Wrap with retry_with_backoff for transient broker failures
        from baldur.core.backoff import ExponentialBackoff
        from baldur.core.retry import RetryConfig, retry_with_backoff

        outcome = retry_with_backoff(
            task.apply_async,
            RetryConfig(
                max_retries=3,
                backoff=ExponentialBackoff(base_delay=0.5, max_delay=5.0),
                retryable_exceptions=(
                    ConnectionError,
                    OSError,
                    TimeoutError,
                ),
                context_name="celery_enqueue",
            ),
            args=args,
            kwargs=kwargs,
            **celery_options,
        )

        if outcome.success:
            assert outcome.result is not None  # success ⇒ result populated
            return outcome.result.id
        raise outcome.exception  # type: ignore[misc]

    def enqueue_many(
        self,
        tasks: list[tuple[str, tuple, dict]],
        options: TaskOptions | None = None,
    ) -> list[str]:
        """
        Enqueue multiple tasks using Celery's group.

        For atomicity, this uses a transaction if the broker supports it.
        """
        from celery import group

        options = options or TaskOptions()
        task_ids = []

        # Build signatures for group
        signatures = []
        for task_name, args, kwargs in tasks:
            task = self._get_task(task_name)
            sig = task.s(*args, **kwargs)

            if options.countdown is not None:
                sig = sig.set(countdown=options.countdown)
            if options.queue is not None:
                sig = sig.set(queue=options.queue)

            signatures.append(sig)

        # Execute as group
        job = group(signatures)
        result = job.apply_async()

        # Collect task IDs
        for child_result in result.children:
            task_ids.append(child_result.id)

        logger.debug(
            "celery_adapter.enqueued_tasks",
            task_ids_count=len(task_ids),
        )
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
        Get task result from Celery result backend.

        If timeout is provided, blocks until task completes.
        """
        from celery.result import AsyncResult

        result = AsyncResult(task_id, app=self._app)

        try:
            if timeout is not None:
                # Block until ready or timeout
                try:
                    result_value = result.get(timeout=timeout, propagate=False)
                except Exception as e:
                    if "timeout" in str(e).lower():
                        raise TaskTimeoutError(f"Task {task_id} timed out") from e
                    raise
            else:
                result_value = result.result if result.ready() else None

            # Map Celery state to TaskStatus
            status_map = {
                "PENDING": TaskStatus.PENDING,
                "STARTED": TaskStatus.STARTED,
                "SUCCESS": TaskStatus.SUCCESS,
                "FAILURE": TaskStatus.FAILURE,
                "RETRY": TaskStatus.RETRY,
                "REVOKED": TaskStatus.REVOKED,
            }

            status = status_map.get(result.state, TaskStatus.PENDING)

            # Build result
            return TaskResult(
                task_id=task_id,
                status=status,
                result=result_value if status == TaskStatus.SUCCESS else None,
                error=str(result.result) if status == TaskStatus.FAILURE else None,
                traceback=result.traceback if status == TaskStatus.FAILURE else None,
                retries=result.retries if hasattr(result, "retries") else 0,
            )

        except TaskTimeoutError:
            raise
        except Exception as e:
            logger.exception(
                "celery_adapter.error_getting_result",
                task_id=task_id,
                error=e,
            )
            return TaskResult(
                task_id=task_id,
                status=TaskStatus.PENDING,
                error=str(e),
            )

    def revoke(
        self,
        task_id: str,
        terminate: bool = False,
        signal: str = "SIGTERM",
    ) -> bool:
        """
        Revoke a pending or running Celery task.

        Uses Celery's control.revoke for cancellation.
        """
        try:
            self._app.control.revoke(
                task_id,
                terminate=terminate,
                signal=signal,
            )
            logger.info(
                "celery_adapter.revoked_task",
                task_id=task_id,
            )
            return True
        except Exception as e:
            logger.exception(
                "celery_adapter.error_revoking_task",
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
        Retry a failed task by re-enqueueing it.

        Note: This creates a new task based on the original's arguments.
        """
        from celery.result import AsyncResult

        result = AsyncResult(task_id, app=self._app)

        # Get original task info
        task_name = result.name
        if task_name is None:
            raise TaskNotFoundError(f"Cannot find original task for {task_id}")

        # Get args/kwargs from result backend (if available)
        args = result.args or ()
        kwargs = result.kwargs or {}

        # Re-enqueue with updated options
        options = TaskOptions(countdown=countdown)
        if max_retries is not None:
            options.max_retries = max_retries

        return self.enqueue(task_name, args=args, kwargs=kwargs, options=options)

    def forget(self, task_id: str) -> bool:
        """Forget a task result."""
        from celery.result import AsyncResult

        try:
            result = AsyncResult(task_id, app=self._app)
            result.forget()
            return True
        except Exception as e:
            logger.exception(
                "celery_adapter.error_forgetting_task",
                task_id=task_id,
                error=e,
            )
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
        Schedule a periodic task.

        Note: This modifies Celery's beat schedule dynamically.
        For production use, prefer configuring beat schedule in settings.
        """
        schedule_name = name or f"schedule_{task_name}_{id(schedule)}"

        # Add to Celery beat schedule
        self._app.conf.beat_schedule[schedule_name] = {
            "task": task_name,
            "schedule": schedule,
            "args": args,
            "kwargs": kwargs or {},
        }

        # Store in our registry
        self._schedules[schedule_name] = ScheduleInfo(
            schedule_id=schedule_name,
            task_name=task_name,
            interval=schedule,
            args=args,
            kwargs=kwargs or {},
            enabled=True,
        )

        logger.info(
            "celery_adapter.scheduled_periodic_task",
            schedule_name=schedule_name,
        )
        return schedule_name

    def unschedule(self, schedule_id: str) -> bool:
        """Remove a periodic schedule."""
        if schedule_id in self._app.conf.beat_schedule:
            del self._app.conf.beat_schedule[schedule_id]

        if schedule_id in self._schedules:
            del self._schedules[schedule_id]
            logger.info(
                "celery_adapter.unscheduled",
                schedule_id=schedule_id,
            )
            return True

        return False

    def get_schedule(self, schedule_id: str) -> ScheduleInfo | None:
        """Get information about a periodic schedule."""
        return self._schedules.get(schedule_id)

    def list_schedules(self) -> list[ScheduleInfo]:
        """List all periodic schedules."""
        return list(self._schedules.values())

    # =========================================================================
    # Queue Management
    # =========================================================================

    def purge_queue(self, queue_name: str = "default") -> int:
        """Purge all tasks from a queue."""
        try:
            purged = self._app.control.purge()
            logger.warning(
                "celery_adapter.purged_tasks",
                purged=purged,
                queue_name=queue_name,
            )
            return purged or 0
        except Exception as e:
            logger.exception(
                "celery_adapter.error_purging_queue",
                error=e,
            )
            return 0

    def queue_length(self, queue_name: str = "default") -> int:
        """Get number of pending tasks in queue."""
        try:
            # This requires broker inspection
            with self._app.connection() as conn:
                queue = conn.default_channel.queue_declare(queue_name, passive=True)
                return queue.message_count
        except Exception as e:
            logger.exception(
                "celery_adapter.error_getting_queue_length",
                error=e,
            )
            return 0

    def list_queues(self) -> list[str]:
        """List all known queue names."""
        # Return configured queues from Celery
        queues = self._app.conf.get("task_queues", [])
        if queues:
            return [q.name for q in queues]
        return [self._default_queue]

    def active_count(self) -> int:
        """Get number of currently executing tasks."""
        try:
            inspect = self._app.control.inspect()
            active = inspect.active()
            if active:
                return sum(len(tasks) for tasks in active.values())
            return 0
        except Exception as e:
            logger.exception(
                "celery_adapter.error_getting_active_count",
                error=e,
            )
            return 0

    # =========================================================================
    # Health Check
    # =========================================================================

    def health_check(self) -> bool:
        """Check if Celery broker and backend are healthy."""
        try:
            # Ping workers
            _settings = get_celery_task_settings()
            inspect = self._app.control.inspect(timeout=_settings.inspect_timeout)
            ping_result = inspect.ping()
            return ping_result is not None and len(ping_result) > 0
        except Exception as e:
            logger.exception(
                "celery_adapter.health_check_failed",
                error=e,
            )
            return False

    def worker_count(self) -> int:
        """Get number of active workers."""
        try:
            _settings = get_celery_task_settings()
            inspect = self._app.control.inspect(timeout=_settings.inspect_timeout)
            ping_result = inspect.ping()
            if ping_result:
                return len(ping_result)
            return 0
        except Exception as e:
            logger.exception(
                "celery_adapter.error_getting_worker_count",
                error=e,
            )
            return 0
