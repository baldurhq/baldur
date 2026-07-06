"""
Celery Beat Schedule에 Governance 태스크 등록 테스트.

get_baldur_beat_schedule()에 거버넌스 태스크(긴급 모드 만료 체크)가
올바르게 포함되는지 검증합니다.

Reference:
    - docs/baldur/16_GOVERNANCE_IMPLEMENTATION_ROADMAP.md
    - baldur/tasks/governance.py
"""

from __future__ import annotations

from unittest.mock import patch


class TestGovernanceBeatScheduleIntegration:
    """거버넌스 태스크가 Celery Beat 스케줄에 포함되는지 테스트."""

    def test_governance_schedule_included_by_default(self):
        """기본 설정에서 거버넌스 스케줄이 포함된다."""
        from baldur.adapters.celery.beat_schedule import (
            get_baldur_beat_schedule,
        )

        schedule = get_baldur_beat_schedule()

        # 거버넌스 태스크가 포함되어 있어야 함
        governance_tasks = [
            key
            for key in schedule.keys()
            if "emergency" in key.lower() or "governance" in key.lower()
        ]
        assert len(governance_tasks) > 0, (
            "Governance tasks should be included in beat schedule"
        )

    def test_check_emergency_mode_expiry_task_registered(self):
        """check_emergency_mode_expiry 태스크가 등록되어 있다."""
        from baldur.adapters.celery.beat_schedule import (
            get_baldur_beat_schedule,
        )

        schedule = get_baldur_beat_schedule()

        # check-emergency-mode-expiry 태스크 찾기
        emergency_expiry_task = None
        for _key, config in schedule.items():
            task_name = config.get("task", "")
            if "check_emergency_mode_expiry" in task_name:
                emergency_expiry_task = config
                break

        assert emergency_expiry_task is not None, (
            "check_emergency_mode_expiry task should be registered"
        )
        assert "schedule" in emergency_expiry_task, "Task should have a schedule"

    def test_governance_schedule_can_be_excluded(self):
        """include_governance=False로 거버넌스 스케줄을 제외할 수 있다."""
        from baldur.adapters.celery.beat_schedule import (
            get_baldur_beat_schedule,
        )

        schedule = get_baldur_beat_schedule(include_governance=False)

        # 거버넌스 태스크가 없어야 함
        for key, config in schedule.items():
            task_name = config.get("task", "")
            assert "check_emergency_mode_expiry" not in task_name, (
                f"Governance task should be excluded when include_governance=False: {key}"
            )


class TestGovernanceBeatScheduleConfiguration:
    """거버넌스 Beat 스케줄 설정 테스트."""

    def test_get_governance_beat_schedule_returns_valid_config(self):
        """get_governance_beat_schedule()이 유효한 설정을 반환한다."""
        from baldur.tasks.governance import get_governance_beat_schedule

        schedule = get_governance_beat_schedule()

        assert isinstance(schedule, dict)
        assert len(schedule) > 0, "Should return at least one task"

        for task_name, config in schedule.items():
            assert "task" in config, f"{task_name}: 'task' field required"
            assert "schedule" in config, f"{task_name}: 'schedule' field required"

    def test_emergency_expiry_task_has_correct_interval(self):
        """긴급 모드 만료 체크 태스크가 15분 간격으로 설정되어 있다."""
        from baldur.tasks.governance import get_governance_beat_schedule

        schedule = get_governance_beat_schedule()

        # check-emergency-mode-expiry 찾기
        expiry_task = schedule.get("check-emergency-mode-expiry")
        assert expiry_task is not None, "check-emergency-mode-expiry task should exist"

        # 스케줄 간격 확인 (15분 = 900초)
        task_schedule = expiry_task.get("schedule")
        assert task_schedule is not None

        # crontab 또는 timedelta 형태일 수 있음
        if hasattr(task_schedule, "run_every"):
            # timedelta schedule
            assert task_schedule.run_every.total_seconds() == 900
        elif hasattr(task_schedule, "minute"):
            # crontab schedule - */15
            assert "15" in str(task_schedule.minute) or task_schedule.minute == "*/15"

    def test_emergency_expiry_task_uses_correct_queue(self):
        """긴급 모드 만료 태스크가 적절한 큐를 사용한다."""
        from baldur.tasks.governance import get_governance_beat_schedule

        schedule = get_governance_beat_schedule()
        expiry_task = schedule.get("check-emergency-mode-expiry")

        assert expiry_task is not None

        options = expiry_task.get("options", {})
        queue = options.get("queue")

        # Emergency-mode expiry routes to the shared maintenance queue
        # (sibling: expire-manual-overrides). "governance" was a bespoke queue
        # name with no declared worker -> scheduled-but-unconsumed.
        assert queue == "maintenance", f"Unexpected queue: {queue}"

    def test_approval_metrics_task_uses_metrics_queue(self):
        """Governance approval-metrics refresh routes to the metrics queue."""
        from baldur.tasks.governance import get_governance_beat_schedule

        schedule = get_governance_beat_schedule()
        entry = schedule.get("refresh-governance-approval-metrics")

        assert entry is not None

        options = entry.get("options", {})
        assert options.get("queue") == "metrics", (
            f"Unexpected queue: {options.get('queue')}"
        )
        # metrics queue max-priority is 1
        assert options.get("priority", 0) <= 1


class TestBeatScheduleValidation:
    """Beat 스케줄 유효성 검증 테스트."""

    def test_validate_schedule_includes_governance(self):
        """validate_schedule()이 거버넌스 태스크를 포함하여 검증한다."""
        from baldur.adapters.celery.beat_schedule import validate_schedule

        result = validate_schedule()

        assert result["valid"] is True, f"Schedule should be valid: {result['errors']}"
        assert result["task_count"] > 0

    def test_get_schedule_summary_shows_all_lanes(self):
        """get_schedule_summary()가 모든 레인을 보여준다."""
        from baldur.adapters.celery.beat_schedule import get_schedule_summary

        summary = get_schedule_summary()

        assert "total_tasks" in summary
        assert "by_lane" in summary
        assert summary["total_tasks"] > 0


class TestGovernanceScheduleErrorHandling:
    """거버넌스 스케줄 오류 처리 테스트."""

    @patch("baldur.adapters.celery.beat_schedule.logger")
    def test_logs_debug_on_successful_load(self, mock_logger):
        """거버넌스 스케줄 로드 성공 시 디버그 로그가 기록된다."""
        from baldur.adapters.celery.beat_schedule import (
            get_baldur_beat_schedule,
        )

        get_baldur_beat_schedule()

        # 디버그 로그 호출 확인
        debug_calls = [str(call) for call in mock_logger.debug.call_args_list]
        governance_log_found = any("governance" in call.lower() for call in debug_calls)
        assert governance_log_found, "Should log governance schedule loading"
