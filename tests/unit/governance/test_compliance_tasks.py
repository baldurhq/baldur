"""
📋 Compliance Tasks unit tests

Tests for compliance tasks implementation:
- CollectBaldurMetricsTask

RunComplianceCheckTask tests moved to
tests/dormant/unit/test_compliance_check_task.py (599 D10/D14 — the
compliance feature relocated to the private distribution).
"""

from datetime import UTC, datetime
from unittest.mock import patch

from baldur.tasks.compliance_tasks import (
    COMPLIANCE_TASKS,
    CollectBaldurMetricsTask,
    get_compliance_beat_schedule,
)
from baldur.tasks.notification_policy import (
    NotificationPolicy,
    NotificationTiming,
)

# =============================================================================
# CollectBaldurMetricsTask Tests
# =============================================================================


class TestCollectBaldurMetricsTask:
    """CollectBaldurMetricsTask tests."""

    def test_task_metadata(self):
        """Verify task metadata."""
        task = CollectBaldurMetricsTask()

        assert task.name == "baldur.collect_baldur_metrics"
        assert task.notification_policy.timing == NotificationTiming.AGGREGATED
        assert task.notification_policy.aggregate is True
        # threshold set to infinity so no notification fires
        assert task.notification_policy.threshold == float("inf")

    def test_run_basic(self):
        """Basic metrics collection."""
        task = CollectBaldurMetricsTask()

        with patch.object(task, "run") as mock_run:
            mock_run.return_value = {
                "success": True,
                "metrics_collected": 5,
                "timestamp": datetime.now(UTC).isoformat(),
            }

            result = mock_run()

            assert result["success"] is True
            assert result["metrics_collected"] >= 0

    def test_run_with_all_components(self):
        """Collect metrics from all components."""
        task = CollectBaldurMetricsTask()

        with patch.object(task, "run") as mock_run:
            mock_run.return_value = {
                "success": True,
                "metrics_collected": 10,
                "timestamp": "2026-01-02T00:00:00Z",
            }

            result = mock_run()

            assert result["success"] is True
            assert result["metrics_collected"] == 10

    def test_get_summary_message(self):
        """Message generation."""
        task = CollectBaldurMetricsTask()

        result = {"success": True, "metrics_collected": 10}
        message = task._get_summary_message(result)

        assert "Metrics" in message
        assert "10" in message


# =============================================================================
# Beat Schedule Tests
# =============================================================================


class TestComplianceBeatSchedule:
    """Compliance lane Beat Schedule tests."""

    def test_schedule_contains_all_tasks(self):
        """Verify all OSS-lane tasks are in the schedule."""
        schedule = get_compliance_beat_schedule()

        # generate-finops-report moved to the private finops lane and
        # run-compliance-check to the dormant compliance lane (599 D10)
        assert "collect-baldur-metrics" in schedule
        assert "generate-finops-report" not in schedule
        assert "run-compliance-check" not in schedule

    def test_schedule_queue_assignments(self):
        """Verify queue assignment."""
        schedule = get_compliance_beat_schedule()

        assert schedule["collect-baldur-metrics"]["options"]["queue"] == "metrics"

    def test_schedule_task_names(self):
        """Verify task names."""
        schedule = get_compliance_beat_schedule()

        assert (
            schedule["collect-baldur-metrics"]["task"]
            == "baldur.collect_baldur_metrics"
        )


# =============================================================================
# Task Registry Tests
# =============================================================================


class TestComplianceTaskRegistry:
    """Compliance lane task registry tests."""

    def test_all_tasks_in_registry(self):
        """Verify all OSS-lane tasks are in the registry."""
        # GenerateFinOpsReportTask moved to baldur_pro and
        # RunComplianceCheckTask to baldur_dormant (599 D10)
        assert len(COMPLIANCE_TASKS) == 1

        task_classes = [t.__name__ for t in COMPLIANCE_TASKS]

        assert "CollectBaldurMetricsTask" in task_classes

    def test_all_tasks_have_names(self):
        """Verify all tasks have a name."""
        for task_class in COMPLIANCE_TASKS:
            task = task_class()
            assert task.name.startswith("baldur.")

    def test_all_tasks_have_policies(self):
        """Verify all tasks have a notification policy."""
        for task_class in COMPLIANCE_TASKS:
            task = task_class()
            assert isinstance(task.notification_policy, NotificationPolicy)


# =============================================================================
# Integration-like Tests
# =============================================================================


class TestComplianceTasksIntegration:
    """Compliance lane task integration tests (lightweight)."""

    def test_all_tasks_can_instantiate(self):
        """All tasks can be instantiated."""
        for task_class in COMPLIANCE_TASKS:
            task = task_class()
            assert task is not None
            assert hasattr(task, "run")
            assert hasattr(task, "_get_summary_message")

    def test_notification_channels_configured(self):
        """Verify notification channel configuration."""
        # Metrics collection uses defaults (no notification)
        metrics_task = CollectBaldurMetricsTask()
        assert metrics_task.notification_policy.threshold == float("inf")
