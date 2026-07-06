"""
LeaderScheduler 단위 테스트.

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
    """LeaderScheduler 인스턴스."""
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
# ScheduledJob 테스트
# =============================================================================


class TestScheduledJob:
    """ScheduledJob 데이터클래스 테스트."""

    def test_should_create_with_required_fields(self):
        """필수 필드로 생성되어야 한다."""

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
        """실행 통계를 추적해야 한다."""
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
        """비활성화 상태를 지원해야 한다."""
        job = ScheduledJob(
            name="test-job",
            func=lambda: None,
            interval_seconds=60.0,
            enabled=False,
        )

        assert job.enabled is False


# =============================================================================
# 초기화 테스트
# =============================================================================


class TestLeaderSchedulerInitialization:
    """초기화 테스트."""

    def test_should_initialize_with_resource_name(self, mock_leader_elector):
        """리소스 이름으로 초기화되어야 한다."""
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
        """리더 콜백이 등록되어야 한다."""
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
        """초기에는 작업이 없어야 한다."""
        assert len(scheduler.jobs) == 0


# =============================================================================
# 작업 등록 테스트
# =============================================================================


class TestLeaderSchedulerJobRegistration:
    """작업 등록 테스트."""

    def test_should_add_job(self, scheduler):
        """작업을 추가해야 한다."""
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
        """데코레이터로 작업을 등록해야 한다."""

        @scheduler.job(interval_seconds=30.0)
        def cleanup_task():
            pass

        jobs = scheduler.jobs
        assert len(jobs) == 1
        assert "cleanup_task" in jobs
        assert jobs["cleanup_task"].interval_seconds == 30.0

    def test_should_register_job_via_decorator_with_custom_name(self, scheduler):
        """데코레이터로 커스텀 이름의 작업을 등록해야 한다."""

        @scheduler.job(name="custom-cleanup", interval_seconds=30.0)
        def cleanup_task():
            pass

        jobs = scheduler.jobs
        assert len(jobs) == 1
        assert "custom-cleanup" in jobs

    def test_should_overwrite_job_with_same_name(self, scheduler):
        """동일한 이름의 작업은 덮어쓰기 되어야 한다."""
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
        """작업을 제거해야 한다."""
        scheduler.add_job(
            name="my-job",
            func=lambda: None,
            interval_seconds=60.0,
        )

        assert len(scheduler.jobs) == 1

        scheduler.remove_job("my-job")

        assert len(scheduler.jobs) == 0

    def test_should_enable_job(self, scheduler):
        """작업을 활성화해야 한다."""
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
        """작업을 비활성화해야 한다."""
        scheduler.add_job(
            name="my-job",
            func=lambda: None,
            interval_seconds=60.0,
        )

        assert scheduler.jobs["my-job"].enabled

        scheduler.disable_job("my-job")

        assert not scheduler.jobs["my-job"].enabled


# =============================================================================
# 시작/중지 테스트
# =============================================================================


class TestLeaderSchedulerStartStop:
    """시작/중지 테스트."""

    def test_should_start_elector_on_start(self, scheduler, mock_leader_elector):
        """start() 호출 시 elector가 시작되어야 한다."""
        scheduler.start()

        assert mock_leader_elector.start.called

    def test_should_be_running_after_start(self, scheduler):
        """start() 후에는 실행 중이어야 한다."""
        scheduler.start()

        assert scheduler._running

    def test_should_stop_elector_on_stop(self, scheduler, mock_leader_elector):
        """stop() 호출 시 elector가 중지되어야 한다."""
        scheduler.start()
        scheduler.stop()

        assert mock_leader_elector.stop.called

    def test_should_not_be_running_after_stop(self, scheduler):
        """stop() 후에는 실행 중이 아니어야 한다."""
        scheduler.start()
        scheduler.stop()

        assert not scheduler._running


# =============================================================================
# 작업 실행 테스트
# =============================================================================


class TestLeaderSchedulerJobExecution:
    """작업 실행 테스트."""

    def test_should_execute_job_when_leader(self, mock_leader_elector):
        """리더일 때 작업을 실행해야 한다."""
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

        # 리더가 됨
        mock_leader_elector.is_leader.return_value = True
        for callback in mock_leader_elector._on_become_callbacks:
            callback()

        time.sleep(0.3)  # 실행 대기

        sched.stop()

        assert len(executed) >= 1

    def test_should_not_execute_job_when_not_leader(
        self, scheduler, mock_leader_elector
    ):
        """리더가 아닐 때는 작업을 실행하지 않아야 한다."""
        executed = []

        def my_job():
            executed.append(time.time())

        scheduler.add_job(
            name="test-job",
            func=my_job,
            interval_seconds=0.05,
        )

        scheduler.start()

        # 리더가 아님
        mock_leader_elector.is_leader.return_value = False

        time.sleep(0.2)

        scheduler.stop()

        assert len(executed) == 0

    def test_should_not_execute_disabled_job(self, mock_leader_elector):
        """비활성화된 작업은 실행하지 않아야 한다."""
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
        """실행 횟수를 추적해야 한다."""
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
        """에러 횟수를 추적해야 한다."""

        def failing_job():
            raise Exception("작업 오류")

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
        """마지막 실행 시간을 업데이트해야 한다."""
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
        """리더십을 잃으면 작업 실행을 중지해야 한다."""
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

        # 리더가 됨
        mock_leader_elector.is_leader.return_value = True
        for callback in mock_leader_elector._on_become_callbacks:
            callback()

        time.sleep(0.25)
        count_before_lose = len(executed)

        # 리더십 상실
        mock_leader_elector.is_leader.return_value = False
        for callback in mock_leader_elector._on_lose_callbacks:
            callback()

        time.sleep(0.25)
        count_after_lose = len(executed)

        sched.stop()

        # 리더십 상실 후에는 실행되지 않아야 함
        assert count_after_lose == count_before_lose


# =============================================================================
# 작업 통계 테스트
# =============================================================================


class TestLeaderSchedulerStats:
    """작업 통계 테스트."""

    def test_should_return_all_job_stats(self, scheduler):
        """모든 작업 통계를 반환해야 한다."""
        scheduler.add_job("job1", lambda: None, 60.0)
        scheduler.add_job("job2", lambda: None, 30.0)
        scheduler.add_job("job3", lambda: None, 120.0)

        stats = scheduler.get_job_stats()

        assert len(stats) == 3
        assert "job1" in stats
        assert "job2" in stats
        assert "job3" in stats

    def test_should_return_job_stats_details(self, scheduler):
        """작업 통계 세부사항을 반환해야 한다."""
        scheduler.add_job("job1", lambda: None, 60.0, enabled=True)
        scheduler.add_job("job2", lambda: None, 30.0, enabled=False)

        stats = scheduler.get_job_stats()

        assert stats["job1"]["enabled"] is True
        assert stats["job1"]["interval_seconds"] == 60.0
        assert stats["job2"]["enabled"] is False
