"""
Tests for Decision Logger Skeleton

Verifies that:
- All structures exist
- All functions are callable
- No logs are produced
- No behavior is introduced
"""

import logging

import pytest

from baldur.core.decision_logger import (
    DecisionBoundaryEventType,
    DecisionLogger,
    ReasonCode,
    log_enter_pre_decision_zone,
    log_exit_pre_decision_zone,
    log_intervention_evaluated,
)


@pytest.fixture(autouse=True)
def enable_log_propagation():
    """Enable log propagation for caplog to work."""
    decision_logger = logging.getLogger("baldur.decision_record")
    original_propagate = decision_logger.propagate
    decision_logger.propagate = True
    yield
    decision_logger.propagate = original_propagate


class TestReasonCode:
    """Test ReasonCode enum."""

    def test_reason_codes_exist(self):
        """Verify all required reason codes exist."""
        assert ReasonCode.THRESHOLD_NOT_MET == "THRESHOLD_NOT_MET"
        assert ReasonCode.STABILITY_OK_NO_INTERVENTION == "STABILITY_OK_NO_INTERVENTION"
        assert ReasonCode.POLICY_CONSTRAINT_ACTIVE == "POLICY_CONSTRAINT_ACTIVE"
        assert ReasonCode.INTERVENTION_ALLOWED == "INTERVENTION_ALLOWED"

    def test_only_four_reason_codes(self):
        """Verify exactly 4 reason codes exist."""
        assert len(ReasonCode) == 4


class TestDecisionBoundaryEventType:
    """Test DecisionBoundaryEventType enum."""

    def test_event_types_exist(self):
        """Verify all required event types exist."""
        assert (
            DecisionBoundaryEventType.ENTER_PRE_DECISION_ZONE
            == "ENTER_PRE_DECISION_ZONE"
        )
        assert (
            DecisionBoundaryEventType.INTERVENTION_EVALUATED == "INTERVENTION_EVALUATED"
        )
        assert (
            DecisionBoundaryEventType.EXIT_PRE_DECISION_ZONE == "EXIT_PRE_DECISION_ZONE"
        )

    def test_only_three_event_types(self):
        """Verify exactly 3 event types exist."""
        assert len(DecisionBoundaryEventType) == 3


class TestModuleFunctions:
    """Test module-level functions."""

    def test_log_enter_pre_decision_zone_callable(self):
        """Verify function is callable and returns None."""
        result = log_enter_pre_decision_zone(service_name="test_service")
        assert result is None

    def test_log_enter_pre_decision_zone_with_policy_version(self):
        """Verify function accepts policy_version."""
        result = log_enter_pre_decision_zone(
            service_name="test_service",
            policy_version="v1.0.0",
        )
        assert result is None

    def test_log_intervention_evaluated_allowed(self):
        """Verify function accepts allowed=True."""
        result = log_intervention_evaluated(
            service_name="test_service",
            allowed=True,
            reason=ReasonCode.INTERVENTION_ALLOWED,
        )
        assert result is None

    def test_log_intervention_evaluated_not_allowed(self):
        """Verify function accepts allowed=False with various reasons."""
        for reason in [
            ReasonCode.THRESHOLD_NOT_MET,
            ReasonCode.STABILITY_OK_NO_INTERVENTION,
            ReasonCode.POLICY_CONSTRAINT_ACTIVE,
        ]:
            result = log_intervention_evaluated(
                service_name="test_service",
                allowed=False,
                reason=reason,
            )
            assert result is None

    def test_log_exit_pre_decision_zone_callable(self):
        """Verify function is callable and returns None."""
        result = log_exit_pre_decision_zone(service_name="test_service")
        assert result is None


class TestDecisionLogger:
    """Test DecisionLogger class."""

    def test_initialization(self):
        """Verify class can be instantiated."""
        logger = DecisionLogger(service_name="test_service")
        assert logger._service_name == "test_service"
        assert logger._policy_version is None

    def test_initialization_with_policy_version(self):
        """Verify class accepts policy_version."""
        logger = DecisionLogger(
            service_name="test_service",
            policy_version="v1.0.0",
        )
        assert logger._service_name == "test_service"
        assert logger._policy_version == "v1.0.0"

    def test_enter_pre_decision_zone_returns_none(self):
        """Verify method returns None."""
        logger = DecisionLogger(service_name="test_service")
        result = logger.enter_pre_decision_zone()
        assert result is None

    def test_intervention_evaluated_returns_none(self):
        """Verify method returns None."""
        logger = DecisionLogger(service_name="test_service")
        result = logger.intervention_evaluated(
            allowed=True,
            reason=ReasonCode.INTERVENTION_ALLOWED,
        )
        assert result is None

    def test_exit_pre_decision_zone_returns_none(self):
        """Verify method returns None."""
        logger = DecisionLogger(service_name="test_service")
        result = logger.exit_pre_decision_zone()
        assert result is None


class TestLogOutput:
    """Verify logs are produced with correct structure."""

    def test_logs_produced_with_correct_fields(self):
        """Verify logs are produced with required fields."""
        import json
        from unittest.mock import MagicMock, patch

        mock_logger = MagicMock()

        with patch("baldur.core.decision_logger.logger", mock_logger):
            log_enter_pre_decision_zone(service_name="test_service")
            log_intervention_evaluated(
                service_name="test_service",
                allowed=True,
                reason=ReasonCode.INTERVENTION_ALLOWED,
            )
            log_exit_pre_decision_zone(service_name="test_service")

        assert mock_logger.info.call_count == 3

        # Parse JSON records from the first positional argument of each call
        json_records = []
        for call in mock_logger.info.call_args_list:
            json_records.append(json.loads(call[0][0]))

        # Verify ENTER event
        enter_record = json_records[0]
        assert enter_record["event"] == "ENTER_PRE_DECISION_ZONE"
        assert enter_record["service_name"] == "test_service"
        assert "timestamp" in enter_record

        # Verify INTERVENTION_EVALUATED event
        eval_record = json_records[1]
        assert eval_record["event"] == "INTERVENTION_EVALUATED"
        assert eval_record["allowed"] is True
        assert eval_record["reason"] == "INTERVENTION_ALLOWED"
        assert eval_record["service_name"] == "test_service"
        assert "timestamp" in eval_record

        # Verify EXIT event
        exit_record = json_records[2]
        assert exit_record["event"] == "EXIT_PRE_DECISION_ZONE"
        assert exit_record["service_name"] == "test_service"
        assert "timestamp" in exit_record

    def test_policy_version_included_when_provided(self):
        """Verify policy_version is included when provided."""
        import json
        from unittest.mock import MagicMock, patch

        mock_logger = MagicMock()

        with patch("baldur.core.decision_logger.logger", mock_logger):
            log_enter_pre_decision_zone(
                service_name="test_service",
                policy_version="v1.0.0",
            )

        assert mock_logger.info.call_count == 1
        record = json.loads(mock_logger.info.call_args[0][0])
        assert record["policy_version"] == "v1.0.0"

    def test_only_allowed_fields_present(self):
        """Verify no extra fields beyond specification."""
        import json
        from unittest.mock import MagicMock, patch

        allowed_fields_enter = {"event", "service_name", "policy_version", "timestamp"}
        allowed_fields_eval = {
            "event",
            "allowed",
            "reason",
            "service_name",
            "policy_version",
            "timestamp",
        }

        mock_logger = MagicMock()

        with patch("baldur.core.decision_logger.logger", mock_logger):
            log_enter_pre_decision_zone(service_name="test")
            log_intervention_evaluated(
                service_name="test",
                allowed=False,
                reason=ReasonCode.THRESHOLD_NOT_MET,
            )

        assert mock_logger.info.call_count == 2

        json_records = []
        for call in mock_logger.info.call_args_list:
            json_records.append(json.loads(call[0][0]))

        enter_record = json_records[0]
        assert set(enter_record.keys()) == allowed_fields_enter

        eval_record = json_records[1]
        assert set(eval_record.keys()) == allowed_fields_eval


class TestImportFromCore:
    """Verify imports work from core module."""

    def test_import_from_core(self):
        """Verify all symbols are exported from core.__init__."""
        from baldur.core import (
            DecisionBoundaryEventType,
            DecisionLogger,
            ReasonCode,
            log_enter_pre_decision_zone,
            log_exit_pre_decision_zone,
            log_intervention_evaluated,
        )

        assert ReasonCode is not None
        assert DecisionBoundaryEventType is not None
        assert DecisionLogger is not None
        assert callable(log_enter_pre_decision_zone)
        assert callable(log_intervention_evaluated)
        assert callable(log_exit_pre_decision_zone)
