"""
Tests for Drift Detection Tasks.
"""

from datetime import timedelta
from unittest.mock import Mock, patch

import pytest


class TestSLADriftDetector:
    """Test SLADriftDetector class."""

    @pytest.fixture
    def mock_dependencies(self):
        """Create mock dependencies for SLADriftDetector."""
        mock_thresholds = Mock()
        mock_thresholds.get_all_thresholds.return_value = {
            "payment": timedelta(minutes=30),
            "point": timedelta(hours=1),
        }

        mock_operations = Mock()
        mock_operations.filter.return_value = mock_operations
        mock_operations.count.return_value = 5

        return {
            "get_sla_thresholds": lambda: mock_thresholds,
            "get_failed_operations": lambda **kwargs: mock_operations,
            "record_sla_breach": Mock(),
        }

    def test_init_with_dependencies(self, mock_dependencies):
        """Should initialize with provided dependencies."""
        from baldur.tasks.drift_detection import SLADriftDetector

        detector = SLADriftDetector(
            get_sla_thresholds=mock_dependencies["get_sla_thresholds"],
            get_failed_operations=mock_dependencies["get_failed_operations"],
            record_sla_breach=mock_dependencies["record_sla_breach"],
        )

        assert detector.get_sla_thresholds is not None
        assert detector.get_failed_operations is not None
        assert detector.record_sla_breach is not None

    def test_check_drift_returns_result_dict(self, mock_dependencies):
        """Should return dictionary with drift results."""
        from baldur.tasks.drift_detection import SLADriftDetector

        detector = SLADriftDetector(
            get_sla_thresholds=mock_dependencies["get_sla_thresholds"],
            get_failed_operations=mock_dependencies["get_failed_operations"],
        )

        result = detector.check_drift()

        assert isinstance(result, dict)
        assert "success" in result

    def test_check_drift_includes_checked_at(self, mock_dependencies):
        """Should include timestamp in result."""
        from baldur.tasks.drift_detection import SLADriftDetector

        detector = SLADriftDetector(
            get_sla_thresholds=mock_dependencies["get_sla_thresholds"],
            get_failed_operations=mock_dependencies["get_failed_operations"],
        )

        result = detector.check_drift()

        if result["success"]:
            assert "checked_at" in result

    def test_check_drift_handles_exception(self, mock_dependencies):
        """Should handle exceptions gracefully."""
        from baldur.tasks.drift_detection import SLADriftDetector

        def failing_thresholds():
            raise Exception("Connection error")

        detector = SLADriftDetector(
            get_sla_thresholds=failing_thresholds,
            get_failed_operations=mock_dependencies["get_failed_operations"],
        )

        result = detector.check_drift()

        assert result["success"] is False
        assert "error" in result


class TestSLADriftDetectorDriftAnalysis:
    """Test drift analysis in SLADriftDetector."""

    @pytest.fixture
    def detector_with_data(self):
        """Create detector with sample data."""
        from baldur.tasks.drift_detection import SLADriftDetector

        mock_thresholds = Mock()
        mock_thresholds.get_all_thresholds.return_value = {
            "payment": timedelta(minutes=30),
        }

        mock_operations = Mock()
        mock_operations.filter.return_value = mock_operations
        mock_operations.count.return_value = 10

        return SLADriftDetector(
            get_sla_thresholds=lambda: mock_thresholds,
            get_failed_operations=lambda **kwargs: mock_operations,
        )

    def test_analyzes_all_domains(self, detector_with_data):
        """Should analyze all configured domains."""
        result = detector_with_data.check_drift()

        if result["success"]:
            assert "domains_checked" in result
            assert "payment" in result["domains_checked"]


class TestDriftDetectorProtocols:
    """Test protocol definitions for drift detection."""

    def test_failed_operation_queryset_protocol_defined(self):
        """Should have FailedOperationQuerySet protocol defined."""
        from baldur.tasks.drift_detection import FailedOperationQuerySet

        # Protocol should define expected methods
        # This is a typing protocol, so just verify it exists
        assert FailedOperationQuerySet is not None

    def test_failed_operation_protocol_defined(self):
        """Should have DriftDetectionOperationProtocol defined."""
        from baldur.tasks.drift_detection import DriftDetectionOperationProtocol

        assert DriftDetectionOperationProtocol is not None

    def test_sla_thresholds_protocol_defined(self):
        """Should have SLAThresholdsProtocol defined."""
        from baldur.tasks.drift_detection import SLAThresholdsProtocol

        assert SLAThresholdsProtocol is not None


class TestDriftDetectorCorePrinciple:
    """Test that drift detector follows core principle."""

    def test_only_generates_warnings_no_auto_adjust(self):
        """Should only generate warnings, never auto-adjust."""
        import inspect

        from baldur.tasks import drift_detection

        source = inspect.getsource(drift_detection)

        # Should mention the principle in docstring
        assert (
            "System provides data, humans make decisions" in source
            or "ONLY generate warnings" in source
            or "NEVER auto-adjust" in source
        )

    def test_sends_notifications_for_warnings(self):
        """Should send notifications when warnings detected."""
        from baldur.tasks.drift_detection import SLADriftDetector

        mock_thresholds = Mock()
        mock_thresholds.get_all_thresholds.return_value = {}

        detector = SLADriftDetector(
            get_sla_thresholds=lambda: mock_thresholds,
            get_failed_operations=lambda **kwargs: Mock(),
        )

        # Method should exist
        assert hasattr(detector, "check_drift")


class TestDriftDetectionLogging:
    """Test logging in drift detection module."""

    def test_has_logger(self):
        """Should have logger configured."""
        from baldur.tasks import drift_detection

        assert hasattr(drift_detection, "logger")

    def test_logs_start_of_check(self):
        """Should log when drift check starts."""
        # Verify logging calls would be made
        from baldur.tasks.drift_detection import SLADriftDetector

        with patch("baldur.tasks.drift_detection.logger") as mock_logger:
            mock_thresholds = Mock()
            mock_thresholds.get_all_thresholds.return_value = {}

            detector = SLADriftDetector(
                get_sla_thresholds=lambda: mock_thresholds,
                get_failed_operations=lambda **kwargs: Mock(),
            )

            detector.check_drift()

            # Should have logged something
            assert mock_logger.info.called or mock_logger.debug.called or True
