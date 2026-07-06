"""
Tests for Chaos Scheduler Tasks.
"""

from unittest.mock import Mock, patch


class TestRunScheduledExperiments:
    """Test run_scheduled_experiments function."""

    def test_delegates_to_service(self):
        """Should delegate to ChaosExecutionService."""
        from baldur.tasks.chaos_scheduler import run_scheduled_experiments

        mock_service = Mock()
        mock_result = Mock()
        mock_result.to_dict.return_value = {"status": "success", "experiments_run": 2}
        mock_service.run_scheduled_experiments.return_value = mock_result

        with patch(
            "baldur.services.execution_services.get_chaos_execution_service",
            return_value=mock_service,
        ):
            result = run_scheduled_experiments()

        mock_service.run_scheduled_experiments.assert_called_once()
        assert result == {"status": "success", "experiments_run": 2}

    def test_returns_dict(self):
        """Should return dictionary result."""
        from baldur.tasks.chaos_scheduler import run_scheduled_experiments

        mock_service = Mock()
        mock_result = Mock()
        mock_result.to_dict.return_value = {}
        mock_service.run_scheduled_experiments.return_value = mock_result

        with patch(
            "baldur.services.execution_services.get_chaos_execution_service",
            return_value=mock_service,
        ):
            result = run_scheduled_experiments()

        assert isinstance(result, dict)


class TestGenerateDailyResilienceReport:
    """Test generate_daily_resilience_report function."""

    def test_delegates_to_service(self):
        """Should delegate to ChaosExecutionService."""
        from baldur.tasks.chaos_scheduler import generate_daily_resilience_report

        mock_service = Mock()
        mock_result = Mock()
        mock_result.to_dict.return_value = {
            "report_type": "daily",
            "status": "generated",
        }
        mock_service.generate_daily_report.return_value = mock_result

        with patch(
            "baldur.services.execution_services.get_chaos_execution_service",
            return_value=mock_service,
        ):
            result = generate_daily_resilience_report()

        mock_service.generate_daily_report.assert_called_once()
        assert result == {"report_type": "daily", "status": "generated"}


class TestCleanupExpiredApprovals:
    """Test cleanup_expired_approvals function."""

    def test_delegates_to_service(self):
        """Should delegate to ChaosExecutionService."""
        from baldur.tasks.chaos_scheduler import cleanup_expired_approvals

        mock_service = Mock()
        mock_result = Mock()
        mock_result.to_dict.return_value = {"cleaned_up": 5}
        mock_service.cleanup_expired_approvals.return_value = mock_result

        with patch(
            "baldur.services.execution_services.get_chaos_execution_service",
            return_value=mock_service,
        ):
            result = cleanup_expired_approvals()

        mock_service.cleanup_expired_approvals.assert_called_once()
        assert result == {"cleaned_up": 5}


class TestCheckAndAlertPendingApprovals:
    """Test check_and_alert_pending_approvals function."""

    def test_delegates_to_service(self):
        """Should delegate to ChaosExecutionService."""
        from baldur.tasks.chaos_scheduler import check_and_alert_pending_approvals

        mock_service = Mock()
        mock_result = Mock()
        mock_result.to_dict.return_value = {"alerts_sent": 3}
        # The actual method is check_pending_approvals, not check_and_alert_pending_approvals
        mock_service.check_pending_approvals.return_value = mock_result

        with patch(
            "baldur.services.execution_services.get_chaos_execution_service",
            return_value=mock_service,
        ):
            result = check_and_alert_pending_approvals()

        mock_service.check_pending_approvals.assert_called_once()
        assert result == {"alerts_sent": 3}


class TestThinTaskArchitecture:
    """Test that tasks follow Thin Task, Fat Service architecture."""

    def test_run_scheduled_experiments_is_thin(self):
        """Should have minimal logic in task."""
        import inspect

        from baldur.tasks.chaos_scheduler import run_scheduled_experiments

        source = inspect.getsource(run_scheduled_experiments)

        # Should have imports and delegation, but no complex logic
        assert "get_chaos_execution_service" in source
        assert "to_dict()" in source

    def test_generate_daily_resilience_report_is_thin(self):
        """Should have minimal logic in task."""
        import inspect

        from baldur.tasks.chaos_scheduler import generate_daily_resilience_report

        source = inspect.getsource(generate_daily_resilience_report)

        # Should delegate to service
        assert "generate_daily_report" in source

    def test_cleanup_expired_approvals_is_thin(self):
        """Should have minimal logic in task."""
        import inspect

        from baldur.tasks.chaos_scheduler import cleanup_expired_approvals

        source = inspect.getsource(cleanup_expired_approvals)

        # Should delegate to service
        assert "cleanup_expired_approvals" in source


class TestModuleExports:
    """Test module-level exports."""

    def test_module_has_required_functions(self):
        """Should export required task functions."""
        from baldur.tasks import chaos_scheduler

        expected_functions = [
            "run_scheduled_experiments",
            "generate_daily_resilience_report",
            "cleanup_expired_approvals",
            "check_and_alert_pending_approvals",
        ]

        for func_name in expected_functions:
            assert hasattr(chaos_scheduler, func_name), f"Missing: {func_name}"

    def test_module_has_logger(self):
        """Should have logger configured."""
        from baldur.tasks import chaos_scheduler

        assert hasattr(chaos_scheduler, "logger")
