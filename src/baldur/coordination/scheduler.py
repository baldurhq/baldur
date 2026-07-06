"""
Leader-election-based scheduler.

Ensures that only a single node executes scheduled jobs in a distributed
environment. Through leader election, only one of several pods acts as the
scheduler.

Usage:
    from baldur.coordination.scheduler import LeaderScheduler, ScheduledJob

    scheduler = LeaderScheduler("my-scheduler")

    @scheduler.job(interval_seconds=60)
    def cleanup_job():
        print("Cleanup running...")

    scheduler.start()
    # ...
    scheduler.stop()
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

import structlog

from baldur.coordination.factory import get_leader_elector
from baldur.coordination.shutdown_integration import (
    register_for_graceful_shutdown,
)
from baldur.utils.time import utc_now

if TYPE_CHECKING:
    from baldur.coordination.base import LeaderElector
    from baldur.meta.daemon_worker import (  # noqa: F401
        DaemonWorkerHandle,
    )

logger = structlog.get_logger()

# Default scheduler resource name
DEFAULT_SCHEDULER_RESOURCE = "scheduler"


@dataclass
class ScheduledJob:
    """
    Scheduled job definition.

    Attributes:
        name: Job name
        func: Function to execute
        interval_seconds: Execution interval (seconds)
        enabled: Whether the job is enabled
        last_run: Timestamp of the last run
        run_count: Number of executions
        error_count: Number of errors
    """

    name: str
    func: Callable[[], None]
    interval_seconds: float
    enabled: bool = True
    last_run: datetime | None = field(default=None)
    run_count: int = field(default=0)
    error_count: int = field(default=0)

    def should_run(self) -> bool:
        """Check whether the job should run."""
        if not self.enabled:
            return False
        if self.last_run is None:
            return True

        elapsed = (utc_now() - self.last_run).total_seconds()
        return elapsed >= self.interval_seconds

    def mark_run(self, success: bool = True) -> None:
        """Mark the job as having run."""
        self.last_run = utc_now()
        self.run_count += 1
        if not success:
            self.error_count += 1


class LeaderScheduler:
    """
    Leader-election-based scheduler.

    Only the leader node executes scheduled jobs in a distributed environment.
    Non-leader nodes remain in a standby state.

    Features:
    - Only a single leader executes jobs
    - Automatic failover (another node takes over if the leader fails)
    - Per-job individual interval configuration
    - Graceful shutdown support

    Attributes:
        resource_name: Resource name (leader election key)
        jobs: List of registered jobs
    """

    def __init__(
        self,
        resource_name: str = DEFAULT_SCHEDULER_RESOURCE,
        tick_interval_seconds: float = 1.0,
        elector: LeaderElector | None = None,
    ):
        """
        Initialize.

        Args:
            resource_name: Resource name (leader election key)
            tick_interval_seconds: Scheduler tick interval (seconds)
            elector: Leader elector to use. When ``None`` (default), resolves
                the distributed elector via ``get_leader_elector(resource_name)``
                — the historical behavior, used by the DLQ-consumer path. The
                inline scheduler injects a ``LocalFileLeaderElector`` here so a
                single host elects a leader and runs jobs out-of-box when
                distributed leader election is disabled (the default).
        """
        self._resource_name = resource_name
        self._tick_interval = tick_interval_seconds

        self._elector = (
            elector if elector is not None else get_leader_elector(resource_name)
        )
        self._jobs: dict[str, ScheduledJob] = {}

        self._running = False
        self._scheduler_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._handle: DaemonWorkerHandle | None = None  # impl 489 D9

        # Register callbacks
        self._elector.on_become_leader(self._on_become_leader)
        self._elector.on_lose_leader(self._on_lose_leader)

        # Register for graceful shutdown
        register_for_graceful_shutdown(self._elector)

    @property
    def is_leader(self) -> bool:
        """Whether this node is currently the leader."""
        return self._elector.is_leader()

    @property
    def jobs(self) -> dict[str, ScheduledJob]:
        """List of registered jobs."""
        return self._jobs.copy()

    def register_leader_callbacks(
        self,
        on_become: Callable[[], None] | None = None,
        on_lose: Callable[[], None] | None = None,
    ) -> None:
        """
        Register external callbacks for leader-transition events.

        Adds callbacks to the LeaderElector's on_become_leader /
        on_lose_leader. They run independently of the scheduler's internal
        callbacks.

        Args:
            on_become: Callback invoked when leadership is acquired
            on_lose: Callback invoked when leadership is lost
        """
        if on_become is not None:
            self._elector.on_become_leader(on_become)
        if on_lose is not None:
            self._elector.on_lose_leader(on_lose)

    def job(
        self,
        interval_seconds: float,
        name: str | None = None,
        enabled: bool = True,
    ) -> Callable[[Callable[[], None]], Callable[[], None]]:
        """
        Decorator to register a scheduled job.

        Args:
            interval_seconds: Execution interval (seconds)
            name: Job name (uses the function name if None)
            enabled: Whether the job is enabled

        Returns:
            The decorator function

        Usage:
            @scheduler.job(interval_seconds=60)
            def my_job():
                print("Running...")
        """

        def decorator(func: Callable[[], None]) -> Callable[[], None]:
            job_name = name or func.__name__
            self.add_job(
                name=job_name,
                func=func,
                interval_seconds=interval_seconds,
                enabled=enabled,
            )
            return func

        return decorator

    def add_job(
        self,
        name: str,
        func: Callable[[], None],
        interval_seconds: float,
        enabled: bool = True,
    ) -> ScheduledJob:
        """
        Add a scheduled job.

        Args:
            name: Job name
            func: Function to execute
            interval_seconds: Execution interval (seconds)
            enabled: Whether the job is enabled

        Returns:
            The ScheduledJob instance
        """
        job = ScheduledJob(
            name=name,
            func=func,
            interval_seconds=interval_seconds,
            enabled=enabled,
        )
        self._jobs[name] = job
        logger.info(
            "scheduler.job_registered",
            job_name=name,
            interval_seconds=interval_seconds,
        )
        return job

    def remove_job(self, name: str) -> bool:
        """
        Remove a scheduled job.

        Args:
            name: Job name

        Returns:
            Whether removal succeeded
        """
        if name in self._jobs:
            del self._jobs[name]
            logger.info(
                "scheduler.job_removed",
                job_name=name,
            )
            return True
        return False

    def enable_job(self, name: str) -> bool:
        """Enable a job."""
        if name in self._jobs:
            self._jobs[name].enabled = True
            return True
        return False

    def disable_job(self, name: str) -> bool:
        """Disable a job."""
        if name in self._jobs:
            self._jobs[name].enabled = False
            return True
        return False

    def start(self) -> None:
        """Start the scheduler."""
        if self._running:
            return

        logger.info(
            "scheduler.started",
            resource_name=self._resource_name,
        )
        self._stop_event.clear()
        self._running = True

        # Start leader election
        self._elector.start()

    def stop(self) -> None:
        """Stop the scheduler."""
        from baldur.metrics.recorders.daemon_worker import unregister_daemon_worker

        logger.info(
            "scheduler.stopped",
            resource_name=self._resource_name,
        )
        if self._handle is not None:
            self._handle.is_stopping = True
        self._running = False
        self._stop_event.set()

        # Wait for the scheduler thread to terminate
        from baldur.settings.thread_management import (
            get_thread_management_settings,
        )

        timeout = get_thread_management_settings().join_timeout
        if self._scheduler_thread and self._scheduler_thread.is_alive():
            self._scheduler_thread.join(timeout=timeout)

        unregister_daemon_worker(f"Scheduler-{self._resource_name}")
        if self._scheduler_thread is not None and self._scheduler_thread.is_alive():
            logger.critical(
                "daemon_worker.stop_join_timeout",
                worker_name=f"Scheduler-{self._resource_name}",
                join_timeout_seconds=timeout,
            )

        # Stop leader election
        self._elector.stop()
        logger.info(
            "scheduler.shutdown_completed",
            resource_name=self._resource_name,
        )

    def _on_become_leader(self) -> None:
        """Start the scheduler loop when this node becomes the leader."""
        logger.info("scheduler.became_leader")
        self._start_scheduler_loop()

    def _on_lose_leader(self) -> None:
        """Stop the scheduler loop when this node loses leadership."""
        logger.info("scheduler.lost_leader")

    def _start_scheduler_loop(self) -> None:
        """Start the scheduler loop (in a separate thread)."""
        from baldur.meta.daemon_worker import DaemonWorkerHandle
        from baldur.metrics.recorders.daemon_worker import register_daemon_worker

        if self._scheduler_thread and self._scheduler_thread.is_alive():
            return

        self._spawn_scheduler_thread()
        assert (
            self._scheduler_thread is not None
        )  # populated by _spawn_scheduler_thread
        if self._handle is None:
            self._handle = DaemonWorkerHandle(
                thread=self._scheduler_thread,
                tick_interval_seconds=self._tick_interval,
                restart_callback=self._spawn_scheduler_thread,
            )
            register_daemon_worker(f"Scheduler-{self._resource_name}", self._handle)
        else:
            self._handle.thread = self._scheduler_thread

    def _spawn_scheduler_thread(self) -> None:
        """Construct + start a fresh scheduler thread."""
        self._scheduler_thread = threading.Thread(
            target=self._scheduler_loop_with_crash_capture,
            name=f"Scheduler-{self._resource_name}",
            daemon=True,
        )
        self._scheduler_thread.start()
        if self._handle is not None:
            self._handle.thread = self._scheduler_thread

    def _scheduler_loop_with_crash_capture(self) -> None:
        try:
            self._scheduler_loop()
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as e:
            if self._handle is not None:
                self._handle.record_crash(e)
            raise

    def _scheduler_loop(self) -> None:
        """Scheduler main loop."""
        import time as _time

        logger.info("scheduler.loop_started")

        while self._running and not self._stop_event.is_set():
            iter_start = _time.monotonic()
            try:
                # Check leadership
                if not self._elector.is_leader():
                    logger.debug("scheduler.awaiting_leadership")
                    if self._handle is not None:
                        self._handle.observe_iteration(_time.monotonic() - iter_start)
                        self._handle.heartbeat()
                    self._stop_event.wait(timeout=self._tick_interval)
                    continue

                # Check jobs due to run
                for job in self._jobs.values():
                    if not self._running or not self._elector.is_leader():
                        break

                    if job.should_run():
                        self._execute_job(job)

                if self._handle is not None:
                    self._handle.observe_iteration(_time.monotonic() - iter_start)
                    self._handle.heartbeat()

                # Wait until the next tick
                self._stop_event.wait(timeout=self._tick_interval)

            except Exception as e:
                logger.exception(
                    "scheduler.loop_error",
                    error=e,
                )
                if self._handle is not None:
                    self._handle.heartbeat()
                self._stop_event.wait(timeout=self._tick_interval)

        logger.info("scheduler.loop_ended")

    def _execute_job(self, job: ScheduledJob) -> None:
        """
        Execute a job.

        Args:
            job: The job to execute
        """
        try:
            logger.debug(
                "scheduler.job_execution_started",
                job=job.name,
            )
            job.func()
            job.mark_run(success=True)
            logger.info(
                "scheduler.job_completed",
                job=job.name,
                run_count=job.run_count,
            )

        except Exception as e:
            job.mark_run(success=False)
            logger.exception(
                "scheduler.job_failed",
                job=job.name,
                error=e,
            )

    def get_job_stats(self) -> dict[str, dict]:
        """
        Return statistics for all jobs.

        Returns:
            Dictionary of per-job statistics
        """
        return {
            name: {
                "enabled": job.enabled,
                "interval_seconds": job.interval_seconds,
                "last_run": job.last_run.isoformat() if job.last_run else None,
                "run_count": job.run_count,
                "error_count": job.error_count,
            }
            for name, job in self._jobs.items()
        }


# Singleton instance cache
_scheduler_cache: dict[str, LeaderScheduler] = {}
_scheduler_lock = threading.Lock()


def get_leader_scheduler(
    resource_name: str = DEFAULT_SCHEDULER_RESOURCE,
    elector: LeaderElector | None = None,
) -> LeaderScheduler:
    """
    Return the LeaderScheduler singleton.

    Args:
        resource_name: Resource name
        elector: Leader elector for a *first* construction of this resource's
            singleton. ``None`` (default) keeps the historical
            ``get_leader_elector(resource_name)`` resolution. Ignored when the
            singleton already exists (the elector is fixed at construction).

    Returns:
        The LeaderScheduler instance
    """
    global _scheduler_cache

    if resource_name in _scheduler_cache:
        return _scheduler_cache[resource_name]

    with _scheduler_lock:
        if resource_name in _scheduler_cache:
            return _scheduler_cache[resource_name]

        scheduler = LeaderScheduler(resource_name=resource_name, elector=elector)
        _scheduler_cache[resource_name] = scheduler
        return scheduler


def reset_schedulers() -> None:
    """Reset all schedulers (for testing)."""
    global _scheduler_cache

    with _scheduler_lock:
        for scheduler in _scheduler_cache.values():
            try:
                scheduler.stop()
            except Exception:
                pass
        _scheduler_cache.clear()
