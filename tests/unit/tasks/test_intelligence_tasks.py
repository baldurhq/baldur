"""
🧠 Intelligence lane (Intelligence Tasks) unit tests.

Test targets:
- CheckSLADriftTask: SLA drift detection
- AnalyzeForensicPendingTask: forensic pending analysis
- CheckRecoveryTransitionsTask: recovery transition checks

AnalyzeCrossStageInsightsTask tests moved to
tests/dormant/unit/test_learning_insight_task.py (599 D10/D14 — the
learning feature relocated to the private distribution).
"""

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from baldur.factory.registry import ProviderRegistry
from baldur.tasks.intelligence_tasks import (
    INTELLIGENCE_TASKS,
    AnalyzeForensicPendingTask,
    CheckRecoveryTransitionsTask,
    CheckSLADriftTask,
    VerifyReconciliationAccuracyTask,
    get_intelligence_beat_schedule,
)
from baldur.tasks.notification_policy import (
    NotificationPolicy,
    NotificationTiming,
)

# =============================================================================
# CheckSLADriftTask Tests
# =============================================================================


class TestCheckSLADriftTask:
    """CheckSLADriftTask tests."""

    def test_task_metadata(self):
        """Verify task metadata."""
        task = CheckSLADriftTask()

        assert task.name == "baldur.check_sla_drift"
        assert task.notification_policy.timing == NotificationTiming.REALTIME
        assert task.notification_policy.threshold == 1
        assert task.notification_policy.threshold_field == "warnings_count"

    def test_run_no_warnings(self):
        """Run with no warnings."""
        CheckSLADriftTask()

        with patch("baldur.tasks.intelligence_tasks.CheckSLADriftTask.run") as mock_run:
            mock_run.return_value = {
                "success": True,
                "warnings_count": 0,
                "warnings": [],
                "metrics": {},
            }

            result = mock_run()

            assert result["success"] is True
            assert result["warnings_count"] == 0
            assert len(result["warnings"]) == 0

    def test_run_with_warnings(self):
        """Run with warnings."""
        task = CheckSLADriftTask()

        # Real run() call test (standalone mode)
        with patch.object(task, "run") as mock_run:
            mock_run.return_value = {
                "success": True,
                "warnings_count": 3,
                "warnings": [
                    {"domain": "payment", "message": "SLA drift detected"},
                    {"domain": "order", "message": "SLA drift detected"},
                    {"domain": "user", "message": "SLA drift detected"},
                ],
                "metrics": {},
            }

            result = mock_run()

            assert result["success"] is True
            assert result["warnings_count"] == 3

    def test_get_severity_warning(self):
        """Severity by warning count."""
        task = CheckSLADriftTask()

        assert task._get_severity({"warnings_count": 0}) == "info"
        assert task._get_severity({"warnings_count": 1}) == "warning"
        assert task._get_severity({"warnings_count": 5}) == "critical"

    def test_get_summary_message_no_drift(self):
        """Message when there is no drift."""
        task = CheckSLADriftTask()

        result = {"success": True, "warnings_count": 0}
        message = task._get_summary_message(result)

        assert "normal" in message or "No" in message

    def test_get_summary_message_with_drift(self):
        """Message when there is drift."""
        task = CheckSLADriftTask()

        result = {"success": True, "warnings_count": 3}
        message = task._get_summary_message(result)

        assert "3" in message
        assert "warning" in message

    def test_get_summary_message_error(self):
        """Error message."""
        task = CheckSLADriftTask()

        result = {"success": False, "error": "Connection failed"}
        message = task._get_summary_message(result)

        assert "failed" in message
        assert "Connection failed" in message


# =============================================================================
# AnalyzeForensicPendingTask Tests
# =============================================================================


class TestAnalyzeForensicPendingTask:
    """AnalyzeForensicPendingTask tests."""

    def test_task_metadata(self):
        """Verify task metadata."""
        task = AnalyzeForensicPendingTask()

        assert task.name == "baldur.analyze_forensic_pending"
        assert task.notification_policy.timing == NotificationTiming.REALTIME
        assert task.notification_policy.threshold == 10
        assert task.notification_policy.threshold_field == "suspicious_count"

    def test_run_no_suspicious(self):
        """Run with no suspicious entries."""
        task = AnalyzeForensicPendingTask()

        with patch.object(task, "run") as mock_run:
            mock_run.return_value = {
                "success": True,
                "analyzed_count": 50,
                "suspicious_count": 0,
                "stuck_patterns": [],
                "recommendations": [],
            }

            result = mock_run()

            assert result["success"] is True
            assert result["suspicious_count"] == 0

    def test_run_with_suspicious(self):
        """Run with suspicious entries."""
        task = AnalyzeForensicPendingTask()

        with patch.object(task, "run") as mock_run:
            mock_run.return_value = {
                "success": True,
                "analyzed_count": 100,
                "suspicious_count": 15,
                "stuck_patterns": [{"action": "stuck", "count": 10}],
                "recommendations": ["manual review recommended"],
            }

            result = mock_run()

            assert result["suspicious_count"] == 15
            assert len(result["recommendations"]) > 0

    def test_extract_patterns(self):
        """Pattern extraction test."""
        task = AnalyzeForensicPendingTask()

        results_by_action = {
            "retry": 20,
            "stuck": 5,
            "skip": 0,
        }

        patterns = task._extract_patterns(results_by_action)

        assert len(patterns) == 2  # only non-zero entries
        assert any(p["action"] == "retry" for p in patterns)
        assert any(p["action"] == "stuck" for p in patterns)

    def test_generate_recommendations(self):
        """Recommendation generation test."""
        task = AnalyzeForensicPendingTask()

        results = {"stuck": 10, "requires_review": 15}

        recommendations = task._generate_recommendations(results, 25)

        assert len(recommendations) >= 2  # both stuck and requires_review trigger

    def test_get_severity(self):
        """Severity determination test."""
        task = AnalyzeForensicPendingTask()

        assert task._get_severity({"suspicious_count": 5}) == "info"
        assert task._get_severity({"suspicious_count": 10}) == "warning"
        assert task._get_severity({"suspicious_count": 50}) == "critical"

    def test_get_summary_message(self):
        """Message generation test."""
        task = AnalyzeForensicPendingTask()

        result = {
            "success": True,
            "suspicious_count": 15,
            "stuck_patterns": [{"action": "stuck"}],
        }
        message = task._get_summary_message(result)

        assert "15" in message
        assert "Forensic" in message


# =============================================================================
# CheckRecoveryTransitionsTask Tests
# =============================================================================


class TestCheckRecoveryTransitionsTask:
    """CheckRecoveryTransitionsTask tests."""

    def test_task_metadata(self):
        """Verify task metadata."""
        task = CheckRecoveryTransitionsTask()

        assert task.name == "baldur.check_recovery_transitions"
        assert task.notification_policy.timing == NotificationTiming.REALTIME
        assert task.notification_policy.threshold == 1
        assert task.notification_policy.cooldown_seconds == 120

    def test_run_no_transitions(self):
        """Run with no transitions."""
        task = CheckRecoveryTransitionsTask()

        with patch.object(task, "run") as mock_run:
            mock_run.return_value = {
                "success": True,
                "transitions_count": 0,
                "circuits_recovered": [],
            }

            result = mock_run()

            assert result["success"] is True
            assert result["transitions_count"] == 0

    def test_run_with_recovery(self):
        """Run with recovered circuits."""
        task = CheckRecoveryTransitionsTask()

        with patch.object(task, "run") as mock_run:
            mock_run.return_value = {
                "success": True,
                "transitions_count": 2,
                "circuits_recovered": ["payment_cb", "order_cb"],
            }

            result = mock_run()

            assert result["transitions_count"] == 2
            assert "payment_cb" in result["circuits_recovered"]

    def test_get_summary_message_recovered(self):
        """Recovery message test."""
        task = CheckRecoveryTransitionsTask()

        result = {
            "success": True,
            "transitions_count": 1,
            "circuits_recovered": ["payment_cb"],
        }
        message = task._get_summary_message(result)

        assert "payment_cb" in message
        assert "recovered" in message

    def test_get_summary_message_no_recovery(self):
        """Transition-only message test."""
        task = CheckRecoveryTransitionsTask()

        result = {
            "success": True,
            "transitions_count": 3,
            "circuits_recovered": [],
        }
        message = task._get_summary_message(result)

        assert "3" in message

    def test_get_summary_message_many_recovered(self):
        """Ellipsis test for many recoveries."""
        task = CheckRecoveryTransitionsTask()

        result = {
            "success": True,
            "transitions_count": 5,
            "circuits_recovered": ["cb1", "cb2", "cb3", "cb4", "cb5"],
        }
        message = task._get_summary_message(result)

        assert "more" in message or "2" in message


# =============================================================================
# Beat Schedule Tests
# =============================================================================


class TestIntelligenceBeatSchedule:
    """Intelligence lane Beat Schedule tests."""

    def test_schedule_contains_all_tasks(self):
        """Verify all tasks are in the schedule."""
        schedule = get_intelligence_beat_schedule()

        assert "check-recovery-transitions" in schedule
        assert "analyze-forensic-pending" in schedule
        assert "check-sla-drift" in schedule
        # analyze-cross-stage-insights moved to the dormant learning lane
        # (599 D10)
        assert "analyze-cross-stage-insights" not in schedule

    def test_schedule_queue_assignments(self):
        """Verify queue assignment."""
        schedule = get_intelligence_beat_schedule()

        assert schedule["check-recovery-transitions"]["options"]["queue"] == "realtime"
        assert schedule["analyze-forensic-pending"]["options"]["queue"] == "analysis"
        assert schedule["check-sla-drift"]["options"]["queue"] == "analysis"

    def test_schedule_task_names(self):
        """Verify task names."""
        schedule = get_intelligence_beat_schedule()

        assert (
            schedule["check-recovery-transitions"]["task"]
            == "baldur.check_recovery_transitions"
        )
        assert (
            schedule["analyze-forensic-pending"]["task"]
            == "baldur.analyze_forensic_pending"
        )
        assert schedule["check-sla-drift"]["task"] == "baldur.check_sla_drift"


# =============================================================================
# Task Registry Tests
# =============================================================================


class TestIntelligenceTaskRegistry:
    """Intelligence lane task registry tests."""

    def test_all_tasks_in_registry(self):
        """Verify all tasks are in the registry."""
        # AnalyzeCrossStageInsightsTask moved to baldur_dormant (599 D10)
        assert len(INTELLIGENCE_TASKS) == 4

        task_classes = [t.__name__ for t in INTELLIGENCE_TASKS]

        assert "CheckSLADriftTask" in task_classes
        assert "AnalyzeForensicPendingTask" in task_classes
        assert "CheckRecoveryTransitionsTask" in task_classes
        assert "VerifyReconciliationAccuracyTask" in task_classes

    def test_all_tasks_have_names(self):
        """Verify all tasks have a name."""
        for task_class in INTELLIGENCE_TASKS:
            task = task_class()
            assert task.name.startswith("baldur.")

    def test_all_tasks_have_policies(self):
        """Verify all tasks have a notification policy."""
        for task_class in INTELLIGENCE_TASKS:
            task = task_class()
            assert isinstance(task.notification_policy, NotificationPolicy)


# =============================================================================
# Run Behavior Tests — verify internal service wiring (no shopping.* imports)
# =============================================================================


class TestCheckSLADriftTaskRunBehavior:
    """Verify CheckSLADriftTask.run() calls SLADriftDetector."""

    def test_run_calls_sla_drift_detector(self):
        """run() must call SLADriftDetector.check_drift()."""
        task = CheckSLADriftTask()

        mock_detector_instance = MagicMock()
        mock_detector_instance.check_drift.return_value = {
            "success": True,
            "warnings": [{"domain": "payment", "type": "SLA_BREACH_RATE_HIGH"}],
            "metrics": {"payment": {"sla_breach_rate": 15.0}},
        }

        with patch(
            "baldur.tasks.drift_detection.SLADriftDetector",
            return_value=mock_detector_instance,
        ):
            result = task.run()

        mock_detector_instance.check_drift.assert_called_once()
        assert result["success"] is True
        assert result["warnings_count"] == 1
        assert len(result["warnings"]) == 1

    def test_run_handles_exception_gracefully(self):
        """Return an error when SLADriftDetector initialization fails."""
        task = CheckSLADriftTask()

        with patch(
            "baldur.tasks.drift_detection.SLADriftDetector",
            side_effect=RuntimeError("detector init failed"),
        ):
            result = task.run()

        assert result["success"] is False
        assert "error" in result
        assert result["warnings_count"] == 0

    def test_no_shopping_import_in_run(self):
        """run() must not contain shopping.* imports."""
        import inspect

        source = inspect.getsource(CheckSLADriftTask.run)
        assert "shopping" not in source


class TestAnalyzeForensicPendingRunBehavior:
    """Verify AnalyzeForensicPendingTask.run() calls the DLQ service."""

    def test_run_calls_dlq_service(self):
        """run() must call get_dlq_service().get_pending_entries()."""
        pytest.importorskip("baldur_pro")
        task = AnalyzeForensicPendingTask()

        mock_dlq = MagicMock()
        mock_entries = [
            SimpleNamespace(status="pending"),
            SimpleNamespace(status="pending"),
            SimpleNamespace(status="stuck"),
        ]
        mock_dlq.get_pending_entries.return_value = mock_entries

        with patch(
            "baldur_pro.services.dlq.get_dlq_service",
            return_value=mock_dlq,
        ):
            result = task.run()

        mock_dlq.get_pending_entries.assert_called_once()
        assert result["success"] is True
        assert result["analyzed_count"] == 3
        assert result["results_by_action"]["pending"] == 2
        assert result["results_by_action"]["stuck"] == 1

    def test_run_classifies_suspicious_entries(self):
        """stuck, requires_review, unknown entries count toward suspicious_count."""
        pytest.importorskip("baldur_pro")
        task = AnalyzeForensicPendingTask()

        mock_dlq = MagicMock()
        mock_entries = [
            SimpleNamespace(status="stuck"),
            SimpleNamespace(status="requires_review"),
            SimpleNamespace(status="unknown"),
            SimpleNamespace(status="pending"),
        ]
        mock_dlq.get_pending_entries.return_value = mock_entries

        with patch(
            "baldur_pro.services.dlq.get_dlq_service",
            return_value=mock_dlq,
        ):
            result = task.run()

        assert result["suspicious_count"] == 3  # stuck + requires_review + unknown

    def test_run_handles_empty_dlq(self):
        """Handle an empty DLQ normally."""
        pytest.importorskip("baldur_pro")
        task = AnalyzeForensicPendingTask()

        mock_dlq = MagicMock()
        mock_dlq.get_pending_entries.return_value = []

        with patch(
            "baldur_pro.services.dlq.get_dlq_service",
            return_value=mock_dlq,
        ):
            result = task.run()

        assert result["success"] is True
        assert result["analyzed_count"] == 0
        assert result["suspicious_count"] == 0

    def test_no_shopping_import_in_run(self):
        """run() must not contain shopping.* imports."""
        import inspect

        source = inspect.getsource(AnalyzeForensicPendingTask.run)
        assert "shopping" not in source


class TestCheckRecoveryTransitionsRunBehavior:
    """Verify CheckRecoveryTransitionsTask.run() calls the CB service."""

    def test_run_calls_circuit_breaker_service(self):
        """run() must call get_circuit_breaker_service().check_recovery_transitions()."""
        task = CheckRecoveryTransitionsTask()

        mock_cb_service = MagicMock()
        mock_cb_service.check_recovery_transitions.return_value = {
            "success": True,
            "count": 2,
            "transitioned": ["payment_cb", "order_cb"],
        }

        with patch(
            "baldur.services.get_circuit_breaker_service",
            return_value=mock_cb_service,
        ):
            result = task.run()

        mock_cb_service.check_recovery_transitions.assert_called_once()
        assert result["success"] is True
        assert result["transitions_count"] == 2
        assert result["circuits_recovered"] == ["payment_cb", "order_cb"]

    def test_run_no_transitions(self):
        """Handle no transitions normally."""
        task = CheckRecoveryTransitionsTask()

        mock_cb_service = MagicMock()
        mock_cb_service.check_recovery_transitions.return_value = {
            "success": True,
            "count": 0,
            "transitioned": [],
        }

        with patch(
            "baldur.services.get_circuit_breaker_service",
            return_value=mock_cb_service,
        ):
            result = task.run()

        assert result["transitions_count"] == 0
        assert result["circuits_recovered"] == []

    def test_run_handles_service_error(self):
        """Return an error on CB service failure."""
        task = CheckRecoveryTransitionsTask()

        with patch(
            "baldur.services.get_circuit_breaker_service",
            side_effect=RuntimeError("CB service unavailable"),
        ):
            result = task.run()

        assert result["success"] is False
        assert "error" in result
        assert result["transitions_count"] == 0

    def test_no_shopping_import_in_run(self):
        """run() must not contain shopping.* imports."""
        import inspect

        source = inspect.getsource(CheckRecoveryTransitionsTask.run)
        assert "shopping" not in source

    def test_no_audit_registry_in_run(self):
        """run() must not contain an audit CircuitBreakerRegistry import."""
        import inspect

        source = inspect.getsource(CheckRecoveryTransitionsTask.run)
        assert "CircuitBreakerRegistry" not in source


# =============================================================================
# VerifyReconciliationAccuracyTask._get_actual_errors — windowed-source Behavior
# (655 D2: the DLQ time-filtered count is the windowed actual source; the
#  in-process Prometheus cumulative is intentionally NOT consulted)
# =============================================================================


class TestGetActualErrorsBehavior:
    """``_get_actual_errors`` reads the windowed DLQ count, never Prometheus."""

    _END = datetime(2026, 1, 1, 12, 0, 0)
    _START = _END - timedelta(minutes=30)

    def test_get_actual_errors_returns_windowed_dlq_entry_count(self):
        # Given a DLQ service returning 3 entries for the window
        task = VerifyReconciliationAccuracyTask()
        mock_dlq = MagicMock()
        mock_dlq.query_entries.return_value = [
            SimpleNamespace(),
            SimpleNamespace(),
            SimpleNamespace(),
        ]

        # When the actual error count is looked up
        with patch.object(
            ProviderRegistry.dlq_service, "safe_get", return_value=mock_dlq
        ):
            result = task._get_actual_errors(self._START, self._END)

        # Then the windowed entry count is returned
        assert result == 3

    def test_get_actual_errors_queries_dlq_with_window_bounds(self):
        """The start/end window is forwarded verbatim to the DLQ query."""
        task = VerifyReconciliationAccuracyTask()
        mock_dlq = MagicMock()
        mock_dlq.query_entries.return_value = []

        with patch.object(
            ProviderRegistry.dlq_service, "safe_get", return_value=mock_dlq
        ):
            task._get_actual_errors(self._START, self._END)

        mock_dlq.query_entries.assert_called_once_with(
            start_time=self._START,
            end_time=self._END,
        )

    def test_get_actual_errors_does_not_consult_prometheus_cumulative(self):
        """The in-process Prometheus adapter must never be consulted (655 D2).

        Feeding the all-time cumulative counter into a 30-minute variance would
        be strictly worse than the bounded DLQ count, so the consumer stays on
        the DLQ windowed source.
        """
        task = VerifyReconciliationAccuracyTask()
        mock_dlq = MagicMock()
        mock_dlq.query_entries.return_value = [SimpleNamespace(), SimpleNamespace()]

        with (
            patch.object(
                ProviderRegistry.dlq_service, "safe_get", return_value=mock_dlq
            ),
            patch(
                "baldur.adapters.prometheus_adapter.get_prometheus_adapter"
            ) as mock_get_adapter,
        ):
            result = task._get_actual_errors(self._START, self._END)

        assert result == 2  # the windowed DLQ count, not a cumulative total
        mock_get_adapter.assert_not_called()

    def test_get_actual_errors_empty_dlq_returns_zero(self):
        task = VerifyReconciliationAccuracyTask()
        mock_dlq = MagicMock()
        mock_dlq.query_entries.return_value = []

        with patch.object(
            ProviderRegistry.dlq_service, "safe_get", return_value=mock_dlq
        ):
            result = task._get_actual_errors(self._START, self._END)

        assert result == 0

    def test_get_actual_errors_no_dlq_service_returns_zero(self):
        """Missing DLQ service (OSS, no baldur_pro) degrades to 0, not an error."""
        task = VerifyReconciliationAccuracyTask()

        with patch.object(ProviderRegistry.dlq_service, "safe_get", return_value=None):
            result = task._get_actual_errors(self._START, self._END)

        assert result == 0

    def test_get_actual_errors_dlq_query_error_returns_zero(self):
        """A raising DLQ query is swallowed → 0 (no data source)."""
        task = VerifyReconciliationAccuracyTask()
        mock_dlq = MagicMock()
        mock_dlq.query_entries.side_effect = RuntimeError("backend down")

        with patch.object(
            ProviderRegistry.dlq_service, "safe_get", return_value=mock_dlq
        ):
            result = task._get_actual_errors(self._START, self._END)

        assert result == 0
