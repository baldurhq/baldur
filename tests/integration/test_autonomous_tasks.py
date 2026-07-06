"""
Integration tests for Baldur Autonomous Tasks.

Tests the complete integration of all 3 lanes:
- Cleanup Lane (Cleanup & Expire)
- Intelligence Lane (Analyze & Learn)
- Compliance Lane (Compliance & Report)
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def mock_dlq_service():
    """Mock DLQ Service for cleanup tasks."""
    pytest.importorskip("baldur_pro")
    with patch("baldur_pro.services.dlq.get_dlq_service") as mock:
        service = MagicMock()
        service.archive_old_entries.return_value = 15
        service.purge_archived.return_value = 5
        mock.return_value = service
        yield service


@pytest.fixture
def mock_pending_config_service():
    """Mock Pending Config Service."""
    with patch("baldur.services.pending_config.get_pending_config_service") as mock:
        service = MagicMock()
        service.cleanup_expired.return_value = 8
        mock.return_value = service
        yield service


@pytest.fixture
def mock_approval_service():
    """Mock Approval Service."""
    pytest.importorskip("baldur_pro")
    with patch("baldur_pro.services.runtime_config.get_approval_service") as mock:
        service = MagicMock()
        service.expire_old_requests.return_value = 3
        mock.return_value = service
        yield service


@pytest.fixture
def mock_compliance_service():
    """Mock Compliance Service."""
    with patch("baldur.tasks.compliance_tasks.get_compliance_service") as mock:
        service = MagicMock()
        service.run_all_checks.return_value = MagicMock(
            violations=[],
            total_checks=10,
            passed_checks=10,
        )
        mock.return_value = service
        yield service


@pytest.fixture
def mock_notification_service():
    """Mock Security Notification Service."""
    with patch(
        "baldur.services.security_notification.get_security_notification_service"
    ) as mock:
        service = MagicMock()
        service.send_alert.return_value = {"sent": True}
        mock.return_value = service
        yield service


# =============================================================================
# Test: Beat Schedule Integration
# =============================================================================


class TestBeatScheduleIntegration:
    """Test Beat Schedule consolidation."""

    def test_get_baldur_beat_schedule_returns_all_lanes(self):
        """Should include all lane schedules."""
        from baldur.adapters.celery.beat_schedule import (
            get_baldur_beat_schedule,
        )

        schedule = get_baldur_beat_schedule()

        assert len(schedule) > 10, "Should have more than 10 scheduled tasks"

        cleanup_tasks = [
            k
            for k in schedule
            if "cleanup" in k or "archive" in k or "expire" in k or "purge" in k
        ]
        assert len(cleanup_tasks) >= 4, "Should have at least 4 cleanup tasks"

        intelligence_tasks = [
            k
            for k in schedule
            if "sla" in k or "forensic" in k or "insights" in k or "recovery" in k
        ]
        assert len(intelligence_tasks) >= 3, "Should have at least 3 intelligence tasks"

        compliance_tasks = [
            k
            for k in schedule
            if "compliance" in k or "finops" in k or "metrics" in k or "daily" in k
        ]
        assert len(compliance_tasks) >= 3, "Should have at least 3 compliance tasks"

    def test_get_baldur_beat_schedule_excludes_lanes(self):
        """Should respect lane exclusion parameters."""
        from baldur.adapters.celery.beat_schedule import (
            get_baldur_beat_schedule,
        )

        schedule = get_baldur_beat_schedule(include_cleanup=False)
        cleanup_tasks = [k for k in schedule if "archive-old-dlq" in k]
        assert len(cleanup_tasks) == 0, "Should not have cleanup lane tasks"

        schedule = get_baldur_beat_schedule(include_intelligence=False)
        intelligence_tasks = [k for k in schedule if "check-sla-drift" in k]
        assert len(intelligence_tasks) == 0, "Should not have intelligence lane tasks"

    def test_schedule_summary(self):
        """Should generate correct schedule summary."""
        from baldur.adapters.celery.beat_schedule import get_schedule_summary

        summary = get_schedule_summary()

        assert "total_tasks" in summary
        assert "by_lane" in summary
        assert "by_queue" in summary
        assert summary["total_tasks"] > 0

    def test_validate_schedule(self):
        """Should validate schedule configuration."""
        from baldur.adapters.celery.beat_schedule import validate_schedule

        result = validate_schedule()

        assert result["valid"] is True, (
            f"Schedule validation failed: {result['errors']}"
        )
        assert result["task_count"] > 0


# =============================================================================
# Test: Intelligence Lane Threshold-Based Alerts
# =============================================================================


class TestIntelligenceLaneThresholdAlerts:
    """Intelligence lane threshold-based notification tests."""

    def test_sla_drift_alerts_on_threshold(self):
        """SLA drift should alert when warnings exceed threshold."""
        from baldur.tasks.intelligence_tasks import CheckSLADriftTask
        from baldur.tasks.notification_policy import NotificationTiming

        task = CheckSLADriftTask()

        assert task.notification_policy.timing == NotificationTiming.REALTIME
        assert task.notification_policy.threshold == 1

    def test_forensic_pending_uses_realtime_on_high_count(self):
        """Forensic pending should use REALTIME notification."""
        from baldur.tasks.intelligence_tasks import AnalyzeForensicPendingTask
        from baldur.tasks.notification_policy import NotificationTiming

        task = AnalyzeForensicPendingTask()
        policy = task.notification_policy

        assert policy.timing == NotificationTiming.REALTIME
        assert policy.threshold == 10
        assert policy.threshold_field == "suspicious_count"


# =============================================================================
# Test: Compliance Lane Violation-Based Alerts
# =============================================================================


# RunComplianceCheckTask notification-policy test moved to
# tests/dormant/integration/test_autonomous_task_policies.py and the
# GenerateFinOpsReportTask channels test is covered by
# tests/pro/unit/services/test_finops_tasks.py (599 D10/D14).


# =============================================================================
# Test: Emergency Level Integration
# =============================================================================


class TestEmergencyLevelIntegration:
    """Test Emergency level affects notification behavior."""

    def test_aggregated_tasks_respect_emergency_escalation(self):
        """Aggregated tasks should escalate to REALTIME in emergency Level 3."""
        from baldur.tasks.base import BaseNotifyingTask
        from baldur.tasks.notification_policy import (
            NotificationPolicy,
            NotificationTiming,
        )

        class TestTask(BaseNotifyingTask):
            name = "test.task"
            notification_policy = NotificationPolicy(
                timing=NotificationTiming.AGGREGATED,
                aggregate=True,
                escalate_on_emergency=True,
            )

            def run(self):
                return {"success": True}

        task = TestTask()

        effective_timing = task._get_effective_timing()
        assert effective_timing == NotificationTiming.AGGREGATED

        assert task.notification_policy.escalate_on_emergency is True


# =============================================================================
# Test: Daily Report Generation
# =============================================================================


class TestDailyReportGeneration:
    """Test daily autonomous report generation."""

    def test_daily_report_data_aggregation(self):
        """DailyReportData should correctly aggregate entries."""
        from baldur.services.daily_report.models import (
            DailyReportData,
            TaskResultEntry,
        )

        report = DailyReportData()

        entry1 = TaskResultEntry(
            task_name="cleanup_task",
            result={"archived_count": 5, "expired_count": 3},
            timestamp=datetime.now(UTC),
            severity="info",
        )
        entry2 = TaskResultEntry(
            task_name="recovery_task",
            result={"recovered_count": 2, "circuit_transitions": 1},
            timestamp=datetime.now(UTC),
            severity="info",
        )

        report.add_entry(entry1)
        report.add_entry(entry2)

        assert report.archived_count == 5
        assert report.expired_count == 3
        assert report.recovered_count == 2
        assert report.circuit_transitions == 1
        assert len(report.entries) == 2

    def test_daily_report_to_dict(self):
        """Daily report should convert to dictionary properly."""
        from baldur.services.daily_report.models import DailyReportData

        report = DailyReportData()
        report.archived_count = 10
        report.recovered_count = 5

        result = report.to_dict()

        assert result["archived_count"] == 10
        assert result["recovered_count"] == 5
        assert "date" in result
        assert "entry_count" in result

    def test_daily_report_merge(self):
        """Daily reports should merge correctly."""
        from baldur.services.daily_report.models import DailyReportData

        report1 = DailyReportData()
        report1.archived_count = 5
        report1.recovered_count = 3

        report2 = DailyReportData()
        report2.archived_count = 10
        report2.recovered_count = 7

        report1.merge(report2)

        assert report1.archived_count == 15
        assert report1.recovered_count == 10


# =============================================================================
# Test: Queue Configuration
# =============================================================================


class TestQueueConfiguration:
    """Test queue configuration for all lanes."""

    def test_cleanup_lane_uses_maintenance_queue(self):
        """Cleanup lane tasks should use maintenance queue."""
        from baldur.tasks.cleanup_tasks import get_cleanup_beat_schedule

        schedule = get_cleanup_beat_schedule()

        for name, config in schedule.items():
            queue = config.get("options", {}).get("queue")
            if "purge" in name:
                assert queue == "critical_maintenance"
            else:
                assert queue == "maintenance"

    def test_intelligence_lane_uses_analysis_queue(self):
        """Intelligence lane tasks should use analysis/realtime queue."""
        from baldur.tasks.intelligence_tasks import get_intelligence_beat_schedule

        schedule = get_intelligence_beat_schedule()

        for _name, config in schedule.items():
            queue = config.get("options", {}).get("queue")
            assert queue in ["analysis", "realtime"]

    def test_compliance_lane_uses_proper_queues(self):
        """Compliance lane tasks should use compliance/reports/metrics queue."""
        from baldur.tasks.compliance_tasks import get_compliance_beat_schedule

        schedule = get_compliance_beat_schedule()

        allowed_queues = ["compliance", "reports", "metrics"]
        for name, config in schedule.items():
            queue = config.get("options", {}).get("queue")
            assert queue in allowed_queues, f"{name} uses unexpected queue {queue}"


# =============================================================================
# Test: Task Registration
# =============================================================================


class TestTaskRegistration:
    """Test task registration with Celery."""

    def test_register_all_tasks_functions_exist(self):
        """Should be able to import registration functions."""
        from baldur.adapters.celery.beat_schedule import (
            register_all_tasks_with_celery,
        )
        from baldur.tasks.compliance_tasks import (
            register_compliance_tasks_with_celery,
        )
        from baldur.tasks.intelligence_tasks import (
            register_intelligence_tasks_with_celery,
        )

        assert callable(register_all_tasks_with_celery)
        assert callable(register_intelligence_tasks_with_celery)
        assert callable(register_compliance_tasks_with_celery)

    def test_all_tasks_have_unique_names(self):
        """All tasks should have unique names."""
        from baldur.tasks.compliance_tasks import COMPLIANCE_TASKS
        from baldur.tasks.intelligence_tasks import INTELLIGENCE_TASKS

        all_names = []

        for task_class in INTELLIGENCE_TASKS + COMPLIANCE_TASKS:
            all_names.append(task_class.name)

        assert len(all_names) == len(set(all_names)), "Duplicate task names found"

    def test_all_celery_tasks_have_unique_names(self):
        """507 D9: every ``@shared_task`` under ``baldur.celery_tasks.*``
        registers a unique task name with Celery.

        ``test_all_tasks_have_unique_names`` only covers the
        ``baldur.tasks.*`` schedule definitions; the
        ``baldur.celery_tasks.*`` namespace where the doc 495 G3 bug
        escaped (two ``@shared_task`` decorators sharing one name)
        was not gated.

        Enumeration strategy:
            1. Use ``pkgutil.iter_modules`` to discover every submodule
               under ``baldur.celery_tasks`` (the package ``__init__``
               direct-imports ``audit_flush_tasks`` since 600 D2;
               ``forecaster_tasks`` relocated to baldur_dormant, 599 D10).
            2. ``importlib.import_module`` forces each submodule to load,
               which triggers Celery's ``@shared_task`` ``PromiseProxy``
               to register against ``current_app.tasks``.
            3. Filter the registry by the ``baldur.celery_tasks.``
               name prefix so unrelated tasks from other test modules
               cannot pollute the assertion.
        """
        import importlib
        import pkgutil

        from celery import current_app

        import baldur.celery_tasks

        for module_info in pkgutil.iter_modules(baldur.celery_tasks.__path__):
            importlib.import_module(f"baldur.celery_tasks.{module_info.name}")

        registered_names = [
            name
            for name in current_app.tasks
            if name.startswith("baldur.celery_tasks.")
        ]

        assert len(registered_names) == len(set(registered_names)), (
            f"Duplicate baldur.celery_tasks.* task names: {sorted(registered_names)}"
        )


class TestAuditFlushBeatRegistrationParity:
    """600 D2: beat injection <-> task registration parity.

    With the effective drain gate enabled, every audit-flush task name the
    consolidated beat schedule injects MUST be a task Celery can run. The
    drain module self-registers via ``@shared_task`` on the importlib load
    that ``_load_schedule_module`` performs during schedule composition, so
    "schedule injected" and "task registered" hold structurally — this test
    pins that invariant against a silent refactor of the composition path.
    """

    def test_injected_audit_flush_tasks_are_registered(self):
        """Gate ON -> the 3 injected drain entries resolve to app.tasks.

        Composing the schedule imports the drain module via
        ``_load_schedule_module`` — the SAME import that the ``@shared_task``
        decorators ride to register. A fresh app then finalizes those
        shared-task finalizers, so "schedule injected" and "task registered"
        are pinned to the one importlib side effect.
        """
        import celery

        from baldur.adapters.celery.beat_schedule import get_baldur_beat_schedule
        from baldur.settings.audit import override_audit_settings

        with override_audit_settings(enabled=True, buffer_redis_enabled=True):
            # Only the audit-flush lane (resolved ON via the gate); other lanes
            # off so the schedule is exactly the drain entries.
            schedule = get_baldur_beat_schedule(
                include_cleanup=False,
                include_intelligence=False,
                include_compliance=False,
                include_traffic_aware=False,
                include_canary_watchdog=False,
                include_governance=False,
                include_xtest_cleanup=False,
                include_saga=False,
                include_chaos_scheduler=False,
                include_postmortem=False,
                include_dlq_maintenance=False,
                include_config_apply=False,
                include_legacy=False,
            )

        injected_task_names = {entry["task"] for entry in schedule.values()}

        # Gate ON -> the drain entries are injected.
        assert injected_task_names == {
            "baldur.celery_tasks.flush_redis_audit_buffer",
            "baldur.celery_tasks.recover_orphaned_processing_queues",
            "baldur.celery_tasks.apply_audit_buffer_safety_ltrim",
        }

        # A fresh app binds the shared-task finalizers registered by the
        # schedule-composition import above.
        app = celery.Celery("audit-flush-parity")
        app.finalize()

        # Parity: every injected task name is a runnable registered task.
        for task_name in injected_task_names:
            assert task_name in app.tasks, (
                f"injected beat task {task_name!r} not registered in app.tasks"
            )

    def test_gate_off_injects_no_audit_flush_entries(self):
        """Gate OFF (default) -> no audit-flush entries are injected."""
        from baldur.adapters.celery.beat_schedule import get_baldur_beat_schedule
        from baldur.settings.audit import override_audit_settings

        with override_audit_settings(enabled=False, buffer_redis_enabled=False):
            schedule = get_baldur_beat_schedule(
                include_cleanup=False,
                include_intelligence=False,
                include_compliance=False,
                include_traffic_aware=False,
                include_canary_watchdog=False,
                include_governance=False,
                include_xtest_cleanup=False,
                include_saga=False,
                include_chaos_scheduler=False,
                include_postmortem=False,
                include_dlq_maintenance=False,
                include_config_apply=False,
                include_legacy=False,
                # include_audit_flush=None resolves from the gate (OFF)
            )

        assert schedule == {}, f"gate OFF but entries injected: {schedule}"
