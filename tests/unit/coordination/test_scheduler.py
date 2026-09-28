"""
LeaderScheduler unit tests.

packages/baldur-python/tests/unit/coordination/test_scheduler.py
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

from baldur.coordination.base import LeadershipState
from baldur.coordination.scheduler import (
    LeaderScheduler,
    ScheduledJob,
)

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def mock_leader_elector():
    """LeaderElector Mock."""
    elector = MagicMock()
    elector.is_leader.return_value = False
    elector.state = LeadershipState.NOT_STARTED
    elector.resource_name = "scheduler-test"

    on_become_callbacks = []
    on_lose_callbacks = []

    def on_become_leader(callback):
        on_become_callbacks.append(callback)
        return callback

    def on_lose_leader(callback):
        on_lose_callbacks.append(callback)
        return callback

    elector.on_become_leader.side_effect = on_become_leader
    elector.on_lose_leader.side_effect = on_lose_leader
    elector._on_become_callbacks = on_become_callbacks
    elector._on_lose_callbacks = on_lose_callbacks

    return elector


@pytest.fixture
def scheduler(mock_leader_elector):
    """LeaderScheduler instance."""
    with (
        patch(
            "baldur.coordination.scheduler.get_leader_elector",
            return_value=mock_leader_elector,
        ),
        patch("baldur.coordination.scheduler.register_for_graceful_shutdown"),
    ):
        sched = LeaderScheduler(
            resource_name="scheduler-test",
            tick_interval_seconds=0.05,
        )
        yield sched

        # Cleanup
        try:
            sched.stop()
        except Exception:
            pass


# =============================================================================
# ScheduledJob tests
# =============================================================================


class TestScheduledJob:
    """ScheduledJob dataclass tests."""

    def test_should_create_with_required_fields(self):
        """Creates with the required fields."""

        def job_func():
            pass

        job = ScheduledJob(
            name="test-job",
            func=job_func,
            interval_seconds=60.0,
        )

        assert job.name == "test-job"
        assert job.func == job_func
        assert job.interval_seconds == 60.0
        assert job.enabled is True
        assert job.run_count == 0
        assert job.error_count == 0
        assert job.last_run is None

    def test_should_track_run_statistics(self):
        """Tracks run statistics."""
        job = ScheduledJob(
            name="test-job",
            func=lambda: None,
            interval_seconds=60.0,
            run_count=5,
            error_count=2,
        )

        assert job.run_count == 5
        assert job.error_count == 2

    def test_should_support_disabled_state(self):
        """Supports the disabled state."""
        job = ScheduledJob(
            name="test-job",
            func=lambda: None,
            interval_seconds=60.0,
            enabled=False,
        )

        assert job.enabled is False


# =============================================================================
# Initialization tests
# =============================================================================


class TestLeaderSchedulerInitialization:
    """Initialization tests."""

    def test_should_initialize_with_resource_name(self, mock_leader_elector):
        """Initializes with the resource name."""
        with (
            patch(
                "baldur.coordination.scheduler.get_leader_elector",
                return_value=mock_leader_elector,
            ),
            patch("baldur.coordination.scheduler.register_for_graceful_shutdown"),
        ):
            sched = LeaderScheduler(
                resource_name="my-scheduler",
            )

        assert sched._resource_name == "my-scheduler"

    def test_should_register_leader_callbacks(self, mock_leader_elector):
        """Registers the leader callbacks."""
        with (
            patch(
                "baldur.coordination.scheduler.get_leader_elector",
                return_value=mock_leader_elector,
            ),
            patch("baldur.coordination.scheduler.register_for_graceful_shutdown"),
        ):
            LeaderScheduler(
                resource_name="my-scheduler",
            )

        assert mock_leader_elector.on_become_leader.called
        assert mock_leader_elector.on_lose_leader.called

    def test_should_have_empty_jobs_initially(self, scheduler):
        """Starts with no jobs."""
        assert len(scheduler.jobs) == 0


# =============================================================================
# Job registration tests
# =============================================================================


class TestLeaderSchedulerJobRegistration:
    """Job registration tests."""

    def test_should_add_job(self, scheduler):
        """Adds a job."""
        executed = []

        def my_job():
            executed.append(True)

        scheduler.add_job(
            name="my-job",
            func=my_job,
            interval_seconds=60.0,
        )

        jobs = scheduler.jobs
        assert len(jobs) == 1
        assert "my-job" in jobs
        assert jobs["my-job"].interval_seconds == 60.0

    def test_should_register_job_via_decorator(self, scheduler):
        """Registers a job via the decorator."""

        @scheduler.job(interval_seconds=30.0)
        def cleanup_task():
            pass

        jobs = scheduler.jobs
        assert len(jobs) == 1
        assert "cleanup_task" in jobs
        assert jobs["cleanup_task"].interval_seconds == 30.0

    def test_should_register_job_via_decorator_with_custom_name(self, scheduler):
        """Registers a job with a custom name via the decorator."""

        @scheduler.job(name="custom-cleanup", interval_seconds=30.0)
        def cleanup_task():
            pass

        jobs = scheduler.jobs
        assert len(jobs) == 1
        assert "custom-cleanup" in jobs

    def test_should_overwrite_job_with_same_name(self, scheduler):
        """A job with the same name overwrites the previous one."""
        scheduler.add_job(
            name="my-job",
            func=lambda: None,
            interval_seconds=60.0,
        )

        scheduler.add_job(
            name="my-job",
            func=lambda: None,
            interval_seconds=30.0,
        )

        jobs = scheduler.jobs
        assert len(jobs) == 1
        assert jobs["my-job"].interval_seconds == 30.0

    def test_should_remove_job(self, scheduler):
        """Removes a job."""
        scheduler.add_job(
            name="my-job",
            func=lambda: None,
            interval_seconds=60.0,
        )

        assert len(scheduler.jobs) == 1

        scheduler.remove_job("my-job")

        assert len(scheduler.jobs) == 0

    def test_should_enable_job(self, scheduler):
        """Enables a job."""
        scheduler.add_job(
            name="my-job",
            func=lambda: None,
            interval_seconds=60.0,
            enabled=False,
        )

        assert not scheduler.jobs["my-job"].enabled

        scheduler.enable_job("my-job")

        assert scheduler.jobs["my-job"].enabled

    def test_should_disable_job(self, scheduler):
        """Disables a job."""
        scheduler.add_job(
            name="my-job",
            func=lambda: None,
            interval_seconds=60.0,
        )

        assert scheduler.jobs["my-job"].enabled

        scheduler.disable_job("my-job")

        assert not scheduler.jobs["my-job"].enabled


# =============================================================================
# Start/stop tests
# =============================================================================


class TestLeaderSchedulerStartStop:
    """Start/stop tests."""

    def test_should_start_elector_on_start(self, scheduler, mock_leader_elector):
        """start() starts the elector."""
        scheduler.start()

        assert mock_leader_elector.start.called

    def test_should_be_running_after_start(self, scheduler):
        """Running after start()."""
        scheduler.start()

        assert scheduler._running

    def test_should_stop_elector_on_stop(self, scheduler, mock_leader_elector):
        """stop() stops the elector."""
        scheduler.start()
        scheduler.stop()

        assert mock_leader_elector.stop.called

    def test_should_not_be_running_after_stop(self, scheduler):
        """Not running after stop()."""
        scheduler.start()
        scheduler.stop()

        assert not scheduler._running


# =============================================================================
# Job execution tests
# =============================================================================


class TestLeaderSchedulerJobExecution:
    """Job execution tests."""

    def test_should_execute_job_when_leader(self, mock_leader_elector):
        """Runs jobs while the leader."""
        executed = []

        def my_job():
            executed.append(time.time())

        with (
            patch(
                "baldur.coordination.scheduler.get_leader_elector",
                return_value=mock_leader_elector,
            ),
            patch("baldur.coordination.scheduler.register_for_graceful_shutdown"),
        ):
            sched = LeaderScheduler(
                resource_name="test-scheduler",
                tick_interval_seconds=0.05,
            )
            sched.add_job(
                name="test-job",
                func=my_job,
                interval_seconds=0.1,
            )

        sched.start()

        # Become the leader
        mock_leader_elector.is_leader.return_value = True
        for callback in mock_leader_elector._on_become_callbacks:
            callback()

        time.sleep(0.3)  # Let the job run

        sched.stop()

        assert len(executed) >= 1

    def test_should_not_execute_job_when_not_leader(
        self, scheduler, mock_leader_elector
    ):
        """Does not run jobs while not the leader."""
        executed = []

        def my_job():
            executed.append(time.time())

        scheduler.add_job(
            name="test-job",
            func=my_job,
            interval_seconds=0.05,
        )

        scheduler.start()

        # Not the leader
        mock_leader_elector.is_leader.return_value = False

        time.sleep(0.2)

        scheduler.stop()

        assert len(executed) == 0

    def test_should_not_execute_disabled_job(self, mock_leader_elector):
        """Does not run a disabled job."""
        executed = []

        def my_job():
            executed.append(time.time())

        with (
            patch(
                "baldur.coordination.scheduler.get_leader_elector",
                return_value=mock_leader_elector,
            ),
            patch("baldur.coordination.scheduler.register_for_graceful_shutdown"),
        ):
            sched = LeaderScheduler(
                resource_name="test-scheduler",
                tick_interval_seconds=0.05,
            )
            sched.add_job(
                name="test-job",
                func=my_job,
                interval_seconds=0.1,
                enabled=False,
            )

        sched.start()

        mock_leader_elector.is_leader.return_value = True
        for callback in mock_leader_elector._on_become_callbacks:
            callback()

        time.sleep(0.3)

        sched.stop()

        assert len(executed) == 0

    def test_should_track_run_count(self, mock_leader_elector):
        """Tracks the run count."""
        with (
            patch(
                "baldur.coordination.scheduler.get_leader_elector",
                return_value=mock_leader_elector,
            ),
            patch("baldur.coordination.scheduler.register_for_graceful_shutdown"),
        ):
            sched = LeaderScheduler(
                resource_name="test-scheduler",
                tick_interval_seconds=0.05,
            )
            sched.add_job(
                name="test-job",
                func=lambda: None,
                interval_seconds=0.1,
            )

        sched.start()

        mock_leader_elector.is_leader.return_value = True
        for callback in mock_leader_elector._on_become_callbacks:
            callback()

        time.sleep(0.35)

        sched.stop()

        jobs = sched.jobs
        assert jobs["test-job"].run_count >= 1

    def test_should_track_error_count(self, mock_leader_elector):
        """Tracks the error count."""

        def failing_job():
            raise Exception("job error")

        with (
            patch(
                "baldur.coordination.scheduler.get_leader_elector",
                return_value=mock_leader_elector,
            ),
            patch("baldur.coordination.scheduler.register_for_graceful_shutdown"),
        ):
            sched = LeaderScheduler(
                resource_name="test-scheduler",
                tick_interval_seconds=0.05,
            )
            sched.add_job(
                name="failing-job",
                func=failing_job,
                interval_seconds=0.1,
            )

        sched.start()

        mock_leader_elector.is_leader.return_value = True
        for callback in mock_leader_elector._on_become_callbacks:
            callback()

        time.sleep(0.25)

        sched.stop()

        jobs = sched.jobs
        assert jobs["failing-job"].error_count >= 1

    def test_should_update_last_run_time(self, mock_leader_elector):
        """Updates the last run time."""
        with (
            patch(
                "baldur.coordination.scheduler.get_leader_elector",
                return_value=mock_leader_elector,
            ),
            patch("baldur.coordination.scheduler.register_for_graceful_shutdown"),
        ):
            sched = LeaderScheduler(
                resource_name="test-scheduler",
                tick_interval_seconds=0.05,
            )
            sched.add_job(
                name="test-job",
                func=lambda: None,
                interval_seconds=0.1,
            )

        sched.start()

        mock_leader_elector.is_leader.return_value = True
        for callback in mock_leader_elector._on_become_callbacks:
            callback()

        time.sleep(0.25)

        sched.stop()

        jobs = sched.jobs
        assert jobs["test-job"].last_run is not None

    def test_should_stop_executing_on_lose_leader(self, mock_leader_elector):
        """Stops running jobs after losing leadership."""
        executed = []

        def my_job():
            executed.append(time.time())

        with (
            patch(
                "baldur.coordination.scheduler.get_leader_elector",
                return_value=mock_leader_elector,
            ),
            patch("baldur.coordination.scheduler.register_for_graceful_shutdown"),
        ):
            sched = LeaderScheduler(
                resource_name="test-scheduler",
                tick_interval_seconds=0.05,
            )
            sched.add_job(
                name="test-job",
                func=my_job,
                interval_seconds=0.1,
            )

        sched.start()

        # Become the leader
        mock_leader_elector.is_leader.return_value = True
        for callback in mock_leader_elector._on_become_callbacks:
            callback()

        time.sleep(0.25)
        count_before_lose = len(executed)

        # Lose leadership
        mock_leader_elector.is_leader.return_value = False
        for callback in mock_leader_elector._on_lose_callbacks:
            callback()

        time.sleep(0.25)
        count_after_lose = len(executed)

        sched.stop()

        # Nothing runs after leadership is lost
        assert count_after_lose == count_before_lose


# =============================================================================
# Job statistics tests
# =============================================================================


class TestLeaderSchedulerStats:
    """Job statistics tests."""

    def test_should_return_all_job_stats(self, scheduler):
        """Returns the stats of every job."""
        scheduler.add_job("job1", lambda: None, 60.0)
        scheduler.add_job("job2", lambda: None, 30.0)
        scheduler.add_job("job3", lambda: None, 120.0)

        stats = scheduler.get_job_stats()

        assert len(stats) == 3
        assert "job1" in stats
        assert "job2" in stats
        assert "job3" in stats

    def test_should_return_job_stats_details(self, scheduler):
        """Returns the job stats details."""
        scheduler.add_job("job1", lambda: None, 60.0, enabled=True)
        scheduler.add_job("job2", lambda: None, 30.0, enabled=False)

        stats = scheduler.get_job_stats()

        assert stats["job1"]["enabled"] is True
        assert stats["job1"]["interval_seconds"] == 60.0
        assert stats["job2"]["enabled"] is False


# =============================================================================
# Daemon-worker handle
# =============================================================================


class TestLeaderSchedulerDaemonHandleContract:
    """The leader loop's handle is declared parent-only."""

    def test_leader_loop_handle_is_fork_source_only(
        self, scheduler, mock_leader_elector
    ):
        """The loop runs only in the process that won leadership — a pre-fork
        server's master — so a forked worker must neither report it nor call
        the respawn callback that would start a second leader loop there.
        """
        from baldur.metrics.recorders.daemon_worker import (
            get_registered_daemon_workers,
        )

        scheduler.start()
        for callback in mock_leader_elector._on_become_callbacks:
            callback()

        handle = get_registered_daemon_workers()["Scheduler-scheduler-test"]
        assert handle.fork_source_only is True
        assert handle.restart_callback == scheduler._spawn_scheduler_thread
