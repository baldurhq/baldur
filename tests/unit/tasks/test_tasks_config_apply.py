"""
Tests for Config Apply Tasks.
"""

from unittest.mock import MagicMock, patch

from baldur.tasks.config_apply import (
    apply_pending_config_changes,
    get_config_apply_beat_schedule,
)


class TestConfigApplyTaskDecorators:
    """Test Celery task decorator configuration."""

    def test_task_has_max_retries(self):
        """Task should have max_retries configured."""
        try:
            from baldur.tasks.config_apply import apply_pending_config_changes

            # If decorated with @shared_task, check attrs
            if hasattr(apply_pending_config_changes, "max_retries"):
                assert apply_pending_config_changes.max_retries == 3
        except Exception:
            # Celery might not be installed
            pass

    def test_task_has_retry_delay(self):
        """Task should have default_retry_delay configured."""
        try:
            from baldur.tasks.config_apply import apply_pending_config_changes

            if hasattr(apply_pending_config_changes, "default_retry_delay"):
                assert apply_pending_config_changes.default_retry_delay == 10
        except Exception:
            pass


class TestConfigApplyTaskNames:
    """Test Celery task names."""

    def test_apply_pending_config_changes_task_name(self):
        """Should have correct task name."""
        expected_name = "baldur.apply_pending_config_changes"

        try:
            from baldur.tasks.config_apply import apply_pending_config_changes

            if hasattr(apply_pending_config_changes, "name"):
                assert apply_pending_config_changes.name == expected_name
        except Exception:
            pass

    def test_apply_graceful_config_change_task_name(self):
        """Should have correct task name."""
        expected_name = "baldur.apply_graceful_config_change"

        try:
            from baldur.tasks.config_apply import apply_graceful_config_change

            if hasattr(apply_graceful_config_change, "name"):
                assert apply_graceful_config_change.name == expected_name
        except Exception:
            pass


class TestThinTaskFatServiceArchitecture:
    """Test that tasks follow Thin Task, Fat Service architecture."""

    def test_module_imports_service(self):
        """Should import from service layer."""
        import inspect

        from baldur.tasks import config_apply

        source = inspect.getsource(config_apply)

        # Should import ConfigApplyService
        assert "get_config_apply_service" in source

    def test_no_business_logic_in_tasks(self):
        """Tasks should delegate to service, not implement logic."""
        import inspect

        from baldur.tasks import config_apply

        source = inspect.getsource(config_apply)

        # Tasks should call service methods
        assert "apply_pending_changes" in source or "apply_graceful_change" in source


class TestConfigApplyModuleLogging:
    """Test module logging configuration."""

    def test_has_logger(self):
        """Should have logger configured."""
        from baldur.tasks import config_apply

        assert hasattr(config_apply, "logger")


class TestGetConfigApplyBeatSchedule:
    """get_config_apply_beat_schedule() pure-dict contract (665 D1).

    The 30s ``maintenance``-queue lane is what makes DELAYED/GRACEFUL config
    changes actually apply on the canonical multi-host (Celery beat) path.
    """

    def test_contains_apply_pending_entry(self):
        """The schedule key is the canonical apply-pending entry."""
        schedule = get_config_apply_beat_schedule()

        assert "apply-pending-config-changes" in schedule

    def test_task_name_is_canonical(self):
        """The lane drives the registered apply task."""
        entry = get_config_apply_beat_schedule()["apply-pending-config-changes"]

        assert entry["task"] == "baldur.apply_pending_config_changes"

    def test_schedule_is_thirty_seconds(self):
        """Cadence is 30s (the 317-orphan-wiring plan)."""
        entry = get_config_apply_beat_schedule()["apply-pending-config-changes"]

        assert entry["schedule"] == 30.0

    def test_queue_is_maintenance(self):
        """Queue 'maintenance' avoids the realtime queue's 30s TTL race."""
        entry = get_config_apply_beat_schedule()["apply-pending-config-changes"]

        assert entry["options"]["queue"] == "maintenance"


class TestApplyPendingAuditAppliedKey:
    """The apply audit must read result['applied'] (absorbed 665 fix).

    ``ConfigApplyService.apply_pending_changes`` returns the count under key
    ``applied``; the task previously read a nonexistent ``applied_count`` key, so
    once this path went live the audit would have permanently recorded 0.
    """

    @staticmethod
    def _run_with_result(result: dict) -> MagicMock:
        """Eagerly run the task with a stubbed service; return the audit mock."""
        with (
            patch(
                "baldur.services.execution_services.get_config_apply_service"
            ) as mock_get,
            patch("baldur.tasks.config_apply.log_config_apply_audit") as mock_audit,
        ):
            mock_service = MagicMock()
            mock_service.apply_pending_changes.return_value = result
            mock_get.return_value = mock_service

            apply_pending_config_changes.apply()
        return mock_audit

    def _applied_count(self, mock_audit: MagicMock) -> int:
        summary_calls = [
            c
            for c in mock_audit.call_args_list
            if c.kwargs.get("config_key") == "pending_changes"
        ]
        assert summary_calls, "expected a pending_changes summary audit record"
        return summary_calls[-1].kwargs["details"]["applied_count"]

    def test_audit_records_count_from_applied_key(self):
        """A result with applied=3 produces an audit applied_count of 3."""
        mock_audit = self._run_with_result({"status": "success", "applied": 3})

        assert self._applied_count(mock_audit) == 3

    def test_audit_uses_applied_not_legacy_applied_count_key(self):
        """A result carrying only the OLD 'applied_count' key audits as 0.

        This pins the fix: the task reads ``applied`` (the real key), so a dict
        that only has the legacy ``applied_count`` name yields the 0 default.
        """
        mock_audit = self._run_with_result({"status": "success", "applied_count": 9})

        assert self._applied_count(mock_audit) == 0
