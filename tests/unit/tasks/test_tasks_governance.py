"""
Tests for Governance Tasks.
"""

from unittest.mock import Mock, patch

import pytest


class TestCheckEmergencyModeExpiry:
    """Test check_emergency_mode_expiry function."""

    @pytest.fixture(autouse=True)
    def _require_pro(self):
        pytest.importorskip("baldur_pro")

    def test_delegates_to_governance_service(self):
        """Should delegate to GovernanceService."""
        from baldur.tasks.governance import check_emergency_mode_expiry

        mock_service = Mock()
        mock_result = Mock()
        mock_result.to_dict.return_value = {
            "action": "none",
            "emergency_active": False,
        }
        mock_service.check_emergency_mode_expiry.return_value = mock_result

        with patch(
            "baldur_pro.services.governance.service.get_governance_service",
            return_value=mock_service,
        ):
            result = check_emergency_mode_expiry()

        mock_service.check_emergency_mode_expiry.assert_called_once()
        assert isinstance(result, dict)

    def test_returns_dict(self):
        """Should return dictionary result."""
        from baldur.tasks.governance import check_emergency_mode_expiry

        mock_service = Mock()
        mock_result = Mock()
        mock_result.to_dict.return_value = {"status": "ok"}
        mock_service.check_emergency_mode_expiry.return_value = mock_result

        with patch(
            "baldur_pro.services.governance.service.get_governance_service",
            return_value=mock_service,
        ):
            result = check_emergency_mode_expiry()

        assert isinstance(result, dict)


class TestGetGovernanceBeatSchedule:
    """Test get_governance_beat_schedule function."""

    def test_returns_schedule_dict(self):
        """Should return Celery Beat schedule dictionary."""
        from baldur.tasks.governance import get_governance_beat_schedule

        schedule = get_governance_beat_schedule()

        assert isinstance(schedule, dict)
        assert "check-emergency-mode-expiry" in schedule

    def test_schedule_has_correct_interval(self):
        """Should have 15 minute interval (900 seconds)."""
        from baldur.tasks.governance import get_governance_beat_schedule

        schedule = get_governance_beat_schedule()
        task_config = schedule["check-emergency-mode-expiry"]

        assert task_config["schedule"] == 900.0  # 15 minutes

    def test_schedule_has_task_name(self):
        """Should have correct task name."""
        from baldur.tasks.governance import get_governance_beat_schedule

        schedule = get_governance_beat_schedule()
        task_config = schedule["check-emergency-mode-expiry"]

        expected_name = "baldur.tasks.governance.check_emergency_mode_expiry"
        assert task_config["task"] == expected_name

    def test_schedule_has_queue_option(self):
        """Should specify queue in options."""
        from baldur.tasks.governance import get_governance_beat_schedule

        schedule = get_governance_beat_schedule()
        task_config = schedule["check-emergency-mode-expiry"]

        assert "options" in task_config
        # Routed to the shared maintenance queue — no dedicated "governance"
        # worker is declared (sibling: expire-manual-overrides).
        assert task_config["options"]["queue"] == "maintenance"

    def test_schedule_has_high_priority(self):
        """Should have high priority."""
        from baldur.tasks.governance import get_governance_beat_schedule

        schedule = get_governance_beat_schedule()
        task_config = schedule["check-emergency-mode-expiry"]

        # Priority 3 is high (lower number = higher priority)
        assert task_config["options"]["priority"] == 3


class TestThinTaskArchitecture:
    """Test that governance tasks follow Thin Task, Fat Service architecture."""

    def test_check_emergency_mode_expiry_is_thin(self):
        """Should have minimal logic in task function."""
        import inspect

        from baldur.tasks.governance import check_emergency_mode_expiry

        source = inspect.getsource(check_emergency_mode_expiry)

        # Should just delegate to service
        assert "get_governance_service" in source
        assert "check_emergency_mode_expiry" in source
        assert "to_dict()" in source

    def test_module_docstring_mentions_architecture(self):
        """Module should document Thin Task, Fat Service architecture."""
        from baldur.tasks import governance

        assert governance.__doc__ is not None
        assert (
            "Thin Task" in governance.__doc__ or "service" in governance.__doc__.lower()
        )


class TestCeleryTaskRegistration:
    """Test Celery task registration."""

    def test_celery_task_registered_when_celery_available(self):
        """Should register Celery task when Celery is installed."""
        try:
            from baldur.tasks.governance import check_emergency_mode_expiry_task

            # If we get here, Celery is installed
            assert check_emergency_mode_expiry_task is not None
        except ImportError:
            # Celery not installed, which is fine
            pytest.skip("Celery not installed")

    def test_handles_celery_not_installed(self):
        """Should handle case when Celery is not installed."""
        # Module should import without Celery
        from baldur.tasks import governance

        # Module should be usable
        assert hasattr(governance, "check_emergency_mode_expiry")
        assert hasattr(governance, "get_governance_beat_schedule")


class TestGovernanceModuleLogging:
    """Test logging configuration."""

    def test_has_logger(self):
        """Should have logger configured."""
        from baldur.tasks import governance

        assert hasattr(governance, "logger")
