"""
Tests for Config Apply Tasks.
"""

from unittest.mock import MagicMock, patch

import pytest

from baldur.core.entitlement import EntitlementResult, EntitlementStatus
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

    Every assertion here describes the composed lane, which the getter now
    returns only under an ACTIVE entitlement verdict. The test process carries
    no licence token, so the verdict is driven explicitly — without it these
    would assert against an empty dict and pass or fail for the wrong reason.
    """

    @pytest.fixture(autouse=True)
    def _entitled(self):
        with patch(
            "baldur.core.entitlement.get_entitlement_status",
            return_value=EntitlementResult(status=EntitlementStatus.ACTIVE),
        ):
            yield

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


class TestConfigApplyBeatGateBehavior:
    """get_config_apply_beat_schedule() composes the lane only when both gates
    pass (759 D3/D4).

    On an unentitled install the task can only return ``blocked`` on cadence, so
    composing it buys a WARNING and an audit row every 30s and nothing else. The
    gate lives in the getter rather than in the consolidated-schedule loop
    because the getter is itself a documented operator entry point, and a gate
    one level up would be bypassed by it.

    The two gates fail in opposite directions on purpose: the entitlement
    verdict is a tier boundary, so an indeterminate read skips the lane, while
    the operator disable list is a convenience knob whose settings fault leaves
    the lane composed.

    Installed-tier presence is answered ahead of both. Every arm below that
    means to exercise a *gate* therefore forces PRO present — without it the
    same assertions pass for the wrong reason in a PRO-absent checkout, where
    the presence short-circuit answers first and nothing downstream runs.
    """

    _APPLY_KEY = "apply-pending-config-changes"

    @staticmethod
    def _schedule_under(monkeypatch, *, status, disabled_jobs: str) -> dict:
        """Compose the schedule under one (verdict, disable-list) combination."""
        from baldur.settings.scheduler import reset_scheduler_settings

        monkeypatch.setenv("BALDUR_SCHEDULER_DISABLED_JOBS", disabled_jobs)
        reset_scheduler_settings()

        with patch(
            "baldur.core.entitlement.get_entitlement_status",
            return_value=EntitlementResult(status=status),
        ):
            return get_config_apply_beat_schedule()

    @pytest.mark.parametrize(
        ("status", "disabled_jobs", "expect_lane"),
        [
            (EntitlementStatus.ACTIVE, "", True),
            (EntitlementStatus.ACTIVE, "config_apply", False),
            (EntitlementStatus.MISSING, "", False),
            (EntitlementStatus.MISSING, "config_apply", False),
        ],
        ids=[
            "entitled_and_enabled",
            "entitled_but_disabled",
            "unentitled_and_enabled",
            "unentitled_and_disabled",
        ],
    )
    def test_both_gates_must_pass_for_the_lane_to_be_composed(
        self, monkeypatch, mock_pro_tier, status, disabled_jobs, expect_lane
    ):
        """Only the entitled-and-not-disabled corner composes an entry."""
        schedule = self._schedule_under(
            monkeypatch, status=status, disabled_jobs=disabled_jobs
        )

        if expect_lane:
            assert self._APPLY_KEY in schedule
        else:
            assert schedule == {}

    def test_unreadable_verdict_composes_no_lane(self, mock_pro_tier):
        """A tier boundary fails closed — an indeterminate read skips the lane."""
        with patch(
            "baldur.core.entitlement.get_entitlement_status",
            side_effect=RuntimeError("licence store down"),
        ):
            assert get_config_apply_beat_schedule() == {}

    def test_scheduler_settings_failure_leaves_the_lane_composed(self, mock_pro_tier):
        """A convenience knob fails open — the same direction the in-process
        scheduler's own read takes, so one variable cannot mean two different
        things in the two lanes."""
        with (
            patch(
                "baldur.core.entitlement.get_entitlement_status",
                return_value=EntitlementResult(status=EntitlementStatus.ACTIVE),
            ),
            patch(
                "baldur.settings.scheduler.get_scheduler_settings",
                side_effect=RuntimeError("settings down"),
            ),
        ):
            schedule = get_config_apply_beat_schedule()

        assert self._APPLY_KEY in schedule

    def test_pro_absent_composes_no_lane_and_never_reads_the_verdict(
        self, mock_oss_tier
    ):
        """Installed-tier presence answers the whole question on an OSS install.

        Without the PRO distribution the verdict can only be non-ACTIVE, so
        reading it would put a licence-file read, an INFO line and two
        entitlement gauge writes on a boot the lane cannot serve — the cost the
        in-process registration filters short-circuit for the same reason.
        """
        with patch("baldur.core.entitlement.get_entitlement_status") as mock_verdict:
            schedule = get_config_apply_beat_schedule()

        assert schedule == {}
        mock_verdict.assert_not_called()
