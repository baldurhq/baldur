"""
arq Task Queue Adapter for Baldur System

Async implementation of AsyncTaskQueueInterface using arq.
Provides async-native task queue functionality for FastAPI
and other async-first frameworks.

Requirements:
    - arq>=0.26
    - redis>=4.0
    - croniter>=2.0 (for cron expression parsing)

Related:
    - interfaces/task_queue.py: Interface definition
    - adapters/queues/celery_adapter.py: Sync counterpart
"""

from __future__ import annotations

import asyncio
import traceback as tb_mod
from collections.abc import Callable
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import structlog

from baldur.interfaces.task_queue import (
    AsyncTaskQueueInterface,
    PartialEnqueueError,
    TaskNotFoundError,
    TaskOptions,
    TaskResult,
    TaskStatus,
    TaskTimeoutError,
)
from baldur.settings.arq_task import get_arq_task_settings

if TYPE_CHECKING:
    from arq import ArqRedis
    from arq.connections import RedisSettings
    from arq.cron import CronJob

logger = structlog.get_logger()

# =========================================================================
# Cron / Interval → arq field conversion
# =========================================================================

# Full value ranges for each cron field.
# When a field's expanded values match its full range, pass None to arq
# (meaning "every" — no constraint on that field).
_FIELD_FULL_RANGES: dict[str, set[int]] = {
    "minute": set(range(60)),
    "hour": set(range(24)),
    "day": set(range(1, 32)),
    "month": set(range(1, 13)),
    "weekday": set(range(7)),
}


def _parse_cron_to_arq_fields(cron_expr: str) -> dict[str, set[int] | None]:
    """Convert standard 5-field cron expression to arq cron field kwargs.

    Uses croniter to parse and expand the expression.
    Handles weekday convention conversion:
        cron standard: 0=Sunday, 1=Monday, ..., 6=Saturday (7=Sunday alias)
        Python/arq: 0=Monday, 1=Tuesday, ..., 6=Sunday

    Raises:
        ValueError: If the cron expression is invalid.
    """
    try:
        from croniter import croniter
    except ImportError:
        # from None: suppress chained ImportError — user-actionable message only
        raise ImportError(
            "croniter is required for cron expression parsing. "
            "Install it with: pip install baldur-framework[arq]"
        ) from None

    try:
        expanded = croniter.expand(cron_expr)
    except (ValueError, KeyError) as exc:
        raise ValueError(f"Invalid cron expression: {cron_expr!r} — {exc}") from exc

    # expanded[0] = [[minute_values], [hour_values], [day_values],
    #                 [month_values], [weekday_values]]
    fields = expanded[0]
    arq_field_names = ("minute", "hour", "day", "month", "weekday")
    result: dict[str, set[int] | None] = {}

    for name, values in zip(arq_field_names, fields, strict=False):
        # croniter returns ['*'] for wildcard fields — no constraint
        if values == ["*"]:
            result[name] = None
            continue

        # After the wildcard early-return, croniter guarantees int values.
        int_values: list[int] = [int(v) for v in values]

        if name == "weekday":
            # cron: 0=Sunday → Python/arq: 0=Monday
            # Conversion: arq_weekday = (cron_weekday - 1) % 7
            int_values = [(v - 1) % 7 for v in int_values]

        value_set = set(int_values)
        if value_set == _FIELD_FULL_RANGES[name]:
            result[name] = None
        else:
            result[name] = value_set

    return result


def _interval_to_arq_fields(interval: timedelta) -> dict[str, set[int] | None]:
    """Convert timedelta interval to arq cron fields.

    Only supports intervals that can be evenly expressed as cron:
        - Minutes: must be a divisor of 60 (1,2,3,4,5,6,10,12,15,20,30)
        - Hours: must be a divisor of 24 (1,2,3,4,6,8,12)

    Raises:
        ValueError: If the interval cannot be expressed as cron.
    """
    total_seconds = int(interval.total_seconds())

    if total_seconds < 60:
        raise ValueError(
            f"arq does not support sub-minute intervals ({total_seconds}s). "
            "Use cron expression or external scheduler (APScheduler, K8s CronJob)."
        )

    if total_seconds % 60 != 0:
        raise ValueError(
            f"Interval {interval} has sub-minute precision. "
            "arq cron only supports minute-level granularity."
        )

    total_minutes = total_seconds // 60

    # Minutes-level interval
    if total_minutes < 60:
        if 60 % total_minutes != 0:
            raise ValueError(
                f"{total_minutes}-minute interval cannot be evenly expressed as cron. "
                f"Use a divisor of 60 (1, 2, 3, 4, 5, 6, 10, 12, 15, 20, 30)."
            )
        return {"minute": set(range(0, 60, total_minutes))}

    # Hours-level interval
    if total_minutes % 60 != 0:
        raise ValueError(
            f"Interval {interval} has sub-hour minute remainder. "
            "Use cron expression for complex schedules."
        )

    total_hours = total_minutes // 60
    if 24 % total_hours != 0:
        raise ValueError(
            f"{total_hours}-hour interval cannot be evenly expressed as cron. "
            f"Use a divisor of 24 (1, 2, 3, 4, 6, 8, 12)."
        )
    return {"minute": {0}, "hour": set(range(0, 24, total_hours))}


# Traceback truncation limit — prevents Redis memory bloat
# from excessively long exception tracebacks (enterprise environments).
_MAX_TRACEBACK_LENGTH = 4096


class ArqTaskAdapter(AsyncTaskQueueInterface):
    """
    arq-based async task queue adapter.

    Uses Redis as broker/backend. Designed for FastAPI and
    other async-first frameworks.

    Redis connection:
        Uses arq's own create_pool() / ArqRedis independently.
        Does NOT depend on RedisConnectionFactory (334) — arq manages
        its own connection lifecycle optimized for its event loop model.

    Redis topology:
        arq RedisSettings supports Standalone and Sentinel (sentinel=True).
        Redis Cluster is NOT supported by arq — use Standalone/Sentinel
        or a Cluster-aware proxy (e.g., Envoy, Twemproxy) if needed.
    """

    def __init__(self, redis_settings: RedisSettings | None = None) -> None:
        # Constructor is intentionally sync — no I/O here.
        # All network I/O (Redis connection) happens in startup().
        # This is critical for ProviderRegistry.get_async_queue() which
        # creates instances under threading.Lock (DCL pattern).
        self._redis_settings = redis_settings
        self._pool: ArqRedis | None = None
        self._registered_tasks: dict[str, Callable] = {}
        self._cron_jobs: list[CronJob] = []

    @property
    def provider_name(self) -> str:
        return "arq"

    # =========================================================================
    # Lifecycle
    # =========================================================================

    async def startup(self) -> None:
        """Create Redis connection pool via arq.create_pool()."""
        from arq import create_pool

        self._pool = await create_pool(self._redis_settings)
        logger.info("arq.pool_created")

    async def shutdown(self) -> None:
        """Close Redis connection pool."""
        if self._pool:
            await self._pool.close()
            logger.info("arq.pool_closed")

    def _ensure_pool(self) -> ArqRedis:
        """Return pool or raise if startup() was not called."""
        if self._pool is None:
            raise RuntimeError(
                "ArqTaskAdapter pool not initialized. "
                "Call await adapter.startup() before enqueuing tasks."
            )
        return self._pool

    # =========================================================================
    # Task Registration
    # =========================================================================

    def task(
        self,
        name: str | None = None,
        *,
        max_retries: int = 3,
        timeout: int | None = None,
        queue: str | None = None,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Decorator to register an async function as a task."""

        def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
            task_name = name or f"{func.__module__}.{func.__qualname__}"
            self._registered_tasks[task_name] = func
            func._task_name = task_name  # type: ignore[attr-defined]
            func._max_retries = max_retries  # type: ignore[attr-defined]
            func._timeout = timeout  # type: ignore[attr-defined]
            func._queue = queue  # type: ignore[attr-defined]

            logger.debug(
                "arq.task_registered",
                task_name=task_name,
            )
            return func

        return decorator

    # =========================================================================
    # Task Execution
    # =========================================================================

    async def enqueue(
        self,
        task_name: str,
        args: tuple = (),
        kwargs: dict | None = None,
        options: TaskOptions | None = None,
    ) -> str:
        """Enqueue a task for async execution via arq."""
        if task_name not in self._registered_tasks:
            raise TaskNotFoundError(f"Unknown task: {task_name}")

        pool = self._ensure_pool()
        kwargs = kwargs or {}
        options = options or TaskOptions()

        # arq's `_expires` stub declares `int | float | timedelta | None`, but
        # the runtime accepts datetime too (treats it as an absolute deadline).
        # TaskOptions.expires is datetime|None — cast at the boundary.
        job = await pool.enqueue_job(
            task_name,
            *args,
            _job_id=None,
            _queue_name=options.queue or "arq:queue",
            _defer_by=timedelta(seconds=options.countdown)
            if options.countdown
            else None,
            _defer_until=options.eta,
            _expires=options.expires,  # type: ignore[arg-type]
            _job_try=None,
            **kwargs,
        )
        if job is None:
            raise TaskNotFoundError(
                f"Failed to enqueue task: {task_name} (job already exists or expired)"
            )

        logger.debug(
            "arq.job_enqueued",
            task_name=task_name,
            job_id=job.job_id,
        )
        return job.job_id

    async def enqueue_many(
        self,
        tasks: list[tuple[str, tuple, dict]],
        options: TaskOptions | None = None,
    ) -> list[str]:
        """Enqueue multiple tasks concurrently via chunked asyncio.gather.

        Tasks are split into chunks of ``enqueue_batch_size`` to prevent
        Redis connection pool exhaustion. Each chunk is executed via
        ``asyncio.gather(return_exceptions=True)`` and results are
        classified into succeeded/failed.

        If a chunk's failure ratio exceeds ``enqueue_failure_threshold``,
        remaining chunks are skipped (infrastructure failure signal).

        ``asyncio.CancelledError`` is not treated as a normal failure —
        it propagates immediately to preserve cancellation semantics
        (e.g., K8s graceful shutdown via SIGTERM).

        Args:
            tasks: List of (task_name, args, kwargs) tuples.
            options: Shared execution options applied to all tasks.

        Returns:
            List of task IDs in same order as input (all succeeded).

        Raises:
            PartialEnqueueError: When some tasks fail. Contains both
                ``succeeded`` (list of (index, ID) tuples) and
                ``failed`` (list of (index, exception) tuples) for
                caller-side recovery.
            asyncio.CancelledError: Re-raised immediately when any
                enqueue coroutine is cancelled.
        """
        if not tasks:
            return []

        settings = get_arq_task_settings()
        batch_size = settings.enqueue_batch_size
        failure_threshold = settings.enqueue_failure_threshold
        all_succeeded: list[tuple[int, str]] = []
        all_failed: list[tuple[int, Exception]] = []

        for chunk_start in range(0, len(tasks), batch_size):
            chunk = tasks[chunk_start : chunk_start + batch_size]
            results = await asyncio.gather(
                *(
                    self.enqueue(name, args, kwargs, options)
                    for name, args, kwargs in chunk
                ),
                return_exceptions=True,
            )

            chunk_failure_count = 0
            for i, result in enumerate(results):
                if isinstance(result, asyncio.CancelledError):
                    raise result
                if isinstance(result, Exception):
                    all_failed.append((chunk_start + i, result))
                    chunk_failure_count += 1
                else:
                    # gather(return_exceptions=True) returns the function
                    # result (str here) when no exception was raised.
                    assert isinstance(result, str)
                    all_succeeded.append((chunk_start + i, result))

            if (
                chunk_failure_count
                and chunk_failure_count / len(chunk) >= failure_threshold
            ):
                logger.warning(
                    "arq.enqueue_many_aborted",
                    chunk_start=chunk_start,
                    chunk_failures=chunk_failure_count,
                    chunk_size=len(chunk),
                    threshold=failure_threshold,
                    total_succeeded=len(all_succeeded),
                    total_failed=len(all_failed),
                )
                break

        if all_failed:
            raise PartialEnqueueError(
                succeeded=all_succeeded,
                failed=all_failed,
            )

        logger.debug(
            "arq.enqueue_many_completed",
            total=len(all_succeeded),
        )
        return [task_id for _, task_id in all_succeeded]

    # =========================================================================
    # Task Management
    # =========================================================================

    async def get_result(
        self,
        task_id: str,
        timeout: float | None = None,
    ) -> TaskResult:
        """
        Get task result from arq job info.

        If timeout is provided, polls until the task completes or
        the timeout expires (raises TaskTimeoutError).
        If timeout is None, returns immediately with current status.
        """
        from arq.jobs import Job

        pool = self._ensure_pool()
        job = Job(task_id, redis=pool)

        if timeout is not None:
            try:
                await asyncio.wait_for(job.result(), timeout=timeout)
            except TimeoutError:
                raise TaskTimeoutError(
                    f"Task {task_id} did not complete within {timeout}s"
                ) from None
            except Exception:
                # Task raised an exception — result() re-raises it.
                # Fall through to info() to build the TaskResult.
                pass

        # arq's job.info() returns a JobDef stub at type-time but actually a
        # JobResult at runtime (with status/result/start_time/finish_time).
        # Cast to Any at the boundary so subsequent attribute reads type-check.
        info: Any = await job.info()

        if info is None:
            return TaskResult(task_id=task_id, status=TaskStatus.PENDING)

        status = self._map_status(info.status)

        # Truncate error/traceback to prevent Redis memory bloat.
        # Enterprise tracebacks can exceed 100KB (deep call stacks,
        # chained exceptions). Combined with keep_result TTL, this
        # provides dual-layer memory protection.
        error_str = None
        traceback_str = None
        if status == TaskStatus.FAILURE and info.result is not None:
            error_str = str(info.result)[:_MAX_TRACEBACK_LENGTH]
            if hasattr(info.result, "__traceback__") and info.result.__traceback__:
                full_tb = "".join(
                    tb_mod.format_exception(
                        type(info.result), info.result, info.result.__traceback__
                    )
                )
                traceback_str = full_tb[:_MAX_TRACEBACK_LENGTH]

        return TaskResult(
            task_id=task_id,
            status=status,
            result=info.result if status == TaskStatus.SUCCESS else None,
            error=error_str,
            traceback=traceback_str,
            started_at=info.start_time,
            completed_at=info.finish_time,
        )

    async def revoke(self, task_id: str) -> bool:
        """Cancel a pending task via arq job abort."""
        from arq.jobs import Job

        pool = self._ensure_pool()
        job = Job(task_id, redis=pool)
        revoked = await job.abort()
        logger.debug("arq.job_revoked", job_id=task_id, success=revoked)
        return revoked

    # =========================================================================
    # Queue Management
    # =========================================================================

    async def queue_length(self, queue_name: str = "arq:queue") -> int:
        """Get number of pending tasks in queue."""
        pool = self._ensure_pool()
        return await pool.zcard(queue_name)

    async def health_check(self) -> bool:
        """Check if Redis backend is reachable."""
        try:
            pool = self._ensure_pool()
            return await pool.ping()
        except Exception:
            logger.warning("arq.health_check_failed")
            return False

    # =========================================================================
    # Scheduling
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
        Register a cron job for arq worker.

        All cron schedules operate in UTC.
        arq cron jobs are configured at worker startup,
        not dynamically at runtime. This method registers the
        job in the adapter's cron list, which must be passed
        to the arq Worker via get_worker_settings().
        """
        if cron is None and interval is None:
            raise ValueError("Either cron or interval must be provided")
        if cron is not None and interval is not None:
            raise ValueError("Cannot specify both cron and interval")
        if task_name not in self._registered_tasks:
            raise TaskNotFoundError(f"Unknown task: {task_name}")

        from arq.cron import cron as arq_cron

        if cron is not None:
            try:
                arq_fields = _parse_cron_to_arq_fields(cron)
            except ValueError:
                logger.warning(
                    "arq.cron_parse_failed",
                    task_name=task_name,
                    cron=cron,
                )
                raise
        else:
            try:
                arq_fields = _interval_to_arq_fields(interval)  # type: ignore[arg-type]
            except ValueError:
                logger.warning(
                    "arq.interval_conversion_failed",
                    task_name=task_name,
                    interval=str(interval),
                )
                raise

        # Always wrap to absorb arq's ctx injection.
        # Interface contract: user functions do NOT receive ctx.
        # arq cron calls func(ctx) — wrapper absorbs ctx and
        # forwards captured args/kwargs to the original function.
        original_func = self._registered_tasks[task_name]
        captured_args = args
        captured_kwargs = dict(kwargs) if kwargs else {}

        async def wrapper(ctx: dict) -> Any:  # noqa: ARG001
            return await original_func(*captured_args, **captured_kwargs)

        wrapper.__qualname__ = f"{original_func.__qualname__}[scheduled]"

        # arq's cron() stub declares each field as a scalar (int/bool/...),
        # but the runtime also accepts `set[int]` for multi-value cron fields
        # (minute={0,15,30,45} etc.). The set is what _croniter_to_arq_fields
        # produces.
        cron_job = arq_cron(
            wrapper,
            name=task_name,
            **arq_fields,  # type: ignore[arg-type]
        )
        self._cron_jobs.append(cron_job)

        logger.debug(
            "arq.cron_scheduled",
            task_name=task_name,
            cron=cron,
            interval=str(interval) if interval else None,
        )
        return f"cron:{task_name}"

    # =========================================================================
    # Worker Integration
    # =========================================================================

    def get_worker_settings(self) -> dict[str, Any]:
        """
        Return settings dict for arq Worker.

        Usage in FastAPI app:
            adapter = ArqTaskAdapter(redis_settings)

            class WorkerSettings:
                functions = list(adapter._registered_tasks.values())
                cron_jobs = adapter._cron_jobs
                redis_settings = adapter._redis_settings
        """
        return {
            "functions": list(self._registered_tasks.values()),
            "cron_jobs": self._cron_jobs,
            "redis_settings": self._redis_settings,
        }

    # =========================================================================
    # Internal
    # =========================================================================

    @staticmethod
    def _map_status(arq_status: str | None) -> TaskStatus:
        """Map arq job status string to TaskStatus enum."""
        mapping = {
            "deferred": TaskStatus.PENDING,
            "queued": TaskStatus.PENDING,
            "in_progress": TaskStatus.STARTED,
            "complete": TaskStatus.SUCCESS,
            "not_found": TaskStatus.PENDING,
        }
        return mapping.get(arq_status, TaskStatus.FAILURE)  # type: ignore[arg-type]
