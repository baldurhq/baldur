"""
Tests for Heartbeat (Dead Man's Snitch), Recovery Notification,
and Override Escalation features.
"""

import pytest

pytest.importorskip("baldur_pro")

pytestmark = pytest.mark.requires_pro


import time
from unittest import mock

from baldur.core.config import ErrorBudgetConfig
from baldur_pro.services.error_budget import (
    FreezeDecisionRecorder,
    OverrideType,
)


class TestHeartbeatMetrics:
    """Tests for heartbeat (Dead Man's Snitch) metrics."""

    def test_emit_heartbeat_updates_timestamp(self):
        """emit_heartbeat should update the timestamp gauge."""
        from baldur.services.metrics import (
            baldur_heartbeat_timestamp,
            emit_heartbeat,
        )

        before = time.time()
        emit_heartbeat(component="test_component")
        after = time.time()

        # Verify timestamp is within reasonable bounds
        # The metric should have been set
        metric_value = baldur_heartbeat_timestamp.labels(
            component="test_component"
        )._value.get()

        assert metric_value >= before
        assert metric_value <= after

    def test_emit_heartbeat_increments_counter(self):
        """emit_heartbeat should increment the counter."""
        from baldur.services.metrics import (
            baldur_heartbeat_count,
            emit_heartbeat,
        )

        # Get current value (may be non-zero from other tests)
        initial = baldur_heartbeat_count.labels(component="test_counter")._value.get()

        emit_heartbeat(component="test_counter")
        emit_heartbeat(component="test_counter")

        final = baldur_heartbeat_count.labels(component="test_counter")._value.get()

        assert final == initial + 2


class TestRecoveryAlerts:
    """Tests for recovery notification functionality."""

    def test_record_recovery_alert_increments_counter(self):
        """record_recovery_alert should increment the counter."""
        from baldur.services.metrics import (
            record_recovery_alert,
            recovery_alert_total,
        )

        initial = recovery_alert_total.labels(component="test_recovery")._value.get()

        record_recovery_alert(component="test_recovery")

        final = recovery_alert_total.labels(component="test_recovery")._value.get()

        assert final == initial + 1

    def test_alert_adapter_recovery_method(self):
        """AlertAdapter should have alert_failsafe_recovered method."""
        from baldur.interfaces.alert_adapter import (
            Alert,
            AlertAdapter,
            AlertSeverity,
        )

        class MockAlertAdapter(AlertAdapter):
            def __init__(self):
                self.alerts_sent = []
                self.resolved_keys = []

            def send(self, alert: Alert) -> None:
                self.alerts_sent.append(alert)

            def resolve(self, alert_key: str) -> None:
                self.resolved_keys.append(alert_key)

        adapter = MockAlertAdapter()

        # Call recovery method
        adapter.alert_failsafe_recovered(
            component="error_budget",
            downtime_seconds=300.0,  # 5 minutes
            recovery_reason="Manual restart",
        )

        # Verify alert was sent
        assert len(adapter.alerts_sent) == 1
        alert = adapter.alerts_sent[0]

        assert "RECOVERED" in alert.title
        assert alert.severity == AlertSeverity.INFO
        assert alert.details["downtime_seconds"] == 300.0
        assert alert.details["recovered"] is True

        # Should also resolve the previous failsafe alert
        assert "failsafe:error_budget" in adapter.resolved_keys

    def test_recovery_alert_formats_downtime_correctly(self):
        """Recovery alert should format downtime in readable format."""
        from baldur.interfaces.alert_adapter import Alert, AlertAdapter

        class MockAlertAdapter(AlertAdapter):
            def __init__(self):
                self.alerts_sent = []

            def send(self, alert: Alert) -> None:
                self.alerts_sent.append(alert)

            def resolve(self, alert_key: str) -> None:
                pass

        adapter = MockAlertAdapter()

        # Test seconds
        adapter.alert_failsafe_recovered("test", 30.0, "test")
        assert "30 sec" in adapter.alerts_sent[-1].description

        # Test minutes
        adapter.alert_failsafe_recovered("test", 180.0, "test")
        assert "3.0 min" in adapter.alerts_sent[-1].description

        # Test hours
        adapter.alert_failsafe_recovered("test", 7200.0, "test")
        assert "2.0 hr" in adapter.alerts_sent[-1].description


class TestOverrideEscalation:
    """Tests for override escalation functionality."""

    def test_record_override_escalation_increments_counter(self):
        """record_override_escalation should increment the counter."""
        from baldur.services.metrics import (
            override_escalation_total,
            record_override_escalation,
        )

        initial = override_escalation_total.labels(override_type="hotfix")._value.get()

        record_override_escalation(override_type="hotfix")

        final = override_escalation_total.labels(override_type="hotfix")._value.get()

        assert final == initial + 1

    def test_alert_adapter_escalation_method(self):
        """AlertAdapter should have alert_override_escalation method."""
        from baldur.interfaces.alert_adapter import (
            Alert,
            AlertAdapter,
            AlertSeverity,
        )

        class MockAlertAdapter(AlertAdapter):
            def __init__(self):
                self.alerts_sent = []

            def send(self, alert: Alert) -> None:
                self.alerts_sent.append(alert)

            def resolve(self, alert_key: str) -> None:
                pass

        adapter = MockAlertAdapter()

        adapter.alert_override_escalation(
            override_type="security_patch",
            requester="admin@example.com",
            reason="Critical security fix",
            service_name="payment-service",
            escalation_channel="#governance",
            escalation_mention="@cto @security",
        )

        assert len(adapter.alerts_sent) == 1
        alert = adapter.alerts_sent[0]

        assert "OVERRIDE ESCALATION" in alert.title
        assert alert.severity == AlertSeverity.WARNING
        assert alert.details["override_type"] == "security_patch"
        assert alert.details["requester"] == "admin@example.com"
        assert alert.details["is_escalation"] is True

    @mock.patch("baldur_pro.services.error_budget.recorder._get_error_budget_config")
    def test_recorder_sends_escalation_on_override(self, mock_config):
        """FreezeDecisionRecorder should send escalation on override."""
        mock_config.return_value = {
            "escalation_enabled": True,
            "escalation_channel": "#test-channel",
            "escalation_mention": "@test-user",
        }

        from baldur.interfaces.alert_adapter import Alert, AlertAdapter

        class MockAlertAdapter(AlertAdapter):
            def __init__(self):
                self.escalations = []

            def send(self, alert: Alert) -> None:
                if alert.details.get("is_escalation"):
                    self.escalations.append(alert)

            def resolve(self, alert_key: str) -> None:
                pass

            def alert_override_escalation(self, **kwargs):
                self.escalations.append(kwargs)

        mock_adapter = MockAlertAdapter()

        recorder = FreezeDecisionRecorder(
            advisor=mock.MagicMock(),
            alert_adapter=mock_adapter,
        )

        # Mock the advisor
        recorder.advisor.get_deployment_verdict.return_value = mock.MagicMock(
            budget_status=mock.MagicMock(budget_remaining_percent=10.0),
            status="FREEZE_RECOMMENDED",
        )

        # Approve override
        recorder.record_override_approved(
            decided_by="admin",
            justification="Emergency fix",
            override_type=OverrideType.HOTFIX,
            deployment_name="critical-fix",
        )

        # Should have sent escalation
        assert len(mock_adapter.escalations) == 1
        escalation = mock_adapter.escalations[0]
        assert escalation["override_type"] == "hotfix"
        assert escalation["requester"] == "admin"

    @mock.patch("baldur_pro.services.error_budget.recorder._get_error_budget_config")
    def test_escalation_disabled_does_not_send(self, mock_config):
        """When escalation_enabled is False, no escalation should be sent."""
        mock_config.return_value = {
            "escalation_enabled": False,
        }

        from baldur.interfaces.alert_adapter import Alert, AlertAdapter

        class MockAlertAdapter(AlertAdapter):
            def __init__(self):
                self.escalations = []

            def send(self, alert: Alert) -> None:
                if alert.details.get("is_escalation"):
                    self.escalations.append(alert)

            def resolve(self, alert_key: str) -> None:
                pass

            def alert_override_escalation(self, **kwargs):
                self.escalations.append(kwargs)

        mock_adapter = MockAlertAdapter()

        recorder = FreezeDecisionRecorder(
            advisor=mock.MagicMock(),
            alert_adapter=mock_adapter,
        )
        recorder.advisor.get_deployment_verdict.return_value = mock.MagicMock(
            budget_status=mock.MagicMock(budget_remaining_percent=10.0),
            status="FREEZE_RECOMMENDED",
        )

        recorder.record_override_approved(
            decided_by="admin",
            justification="Emergency fix",
            override_type=OverrideType.HOTFIX,
        )

        # Should NOT have sent escalation
        assert len(mock_adapter.escalations) == 0


class TestErrorBudgetConfigNewFields:
    """Tests for new ErrorBudgetConfig fields."""

    def test_config_has_heartbeat_fields(self):
        """ErrorBudgetConfig should have heartbeat fields.

        Defaults flipped to False per impl 527 (v1.1 deferred).
        """
        config = ErrorBudgetConfig()

        assert hasattr(config, "heartbeat_enabled")
        assert hasattr(config, "heartbeat_interval_seconds")
        assert hasattr(config, "heartbeat_timeout_seconds")

        assert config.heartbeat_enabled is False
        assert config.heartbeat_interval_seconds == 60
        assert config.heartbeat_timeout_seconds == 120

    def test_config_has_recovery_fields(self):
        """ErrorBudgetConfig should have recovery alert fields.

        Default for recovery_alert_enabled flipped to False per impl 527
        (v1.1 deferred).
        """
        config = ErrorBudgetConfig()

        assert hasattr(config, "recovery_alert_enabled")
        assert hasattr(config, "recovery_alert_include_downtime")

        assert config.recovery_alert_enabled is False
        assert config.recovery_alert_include_downtime is True

    def test_config_has_escalation_fields(self):
        """ErrorBudgetConfig should have escalation fields.

        Default for escalation_enabled flipped to False per impl 527
        (v1.1 deferred).
        """
        config = ErrorBudgetConfig()

        assert hasattr(config, "escalation_enabled")
        assert hasattr(config, "escalation_channel")
        assert hasattr(config, "escalation_mention")

        assert config.escalation_enabled is False
        assert config.escalation_channel == "#governance"
        assert config.escalation_mention == "@cto @security"


class TestRuntimeConfigNewFields:
    """Tests for RuntimeConfigManager new fields."""

    def test_update_heartbeat_config(self):
        """RuntimeConfigManager should accept heartbeat settings."""
        from baldur_pro.services.runtime_config import (
            RuntimeConfigManager,
            reset_runtime_config_manager,
        )

        reset_runtime_config_manager()
        manager = RuntimeConfigManager()

        result = manager.update_error_budget_config(
            heartbeat_enabled=True,
            heartbeat_interval_seconds=30,
            heartbeat_timeout_seconds=90,
        )

        assert result["heartbeat_enabled"] is True
        assert result["heartbeat_interval_seconds"] == 30
        assert result["heartbeat_timeout_seconds"] == 90

    def test_update_recovery_config(self):
        """RuntimeConfigManager should accept recovery alert settings."""
        from baldur_pro.services.runtime_config import (
            RuntimeConfigManager,
            reset_runtime_config_manager,
        )

        reset_runtime_config_manager()
        manager = RuntimeConfigManager()

        result = manager.update_error_budget_config(
            recovery_alert_enabled=False,
            recovery_alert_include_downtime=True,
        )

        assert result["recovery_alert_enabled"] is False
        assert result["recovery_alert_include_downtime"] is True

    def test_update_escalation_config(self):
        """RuntimeConfigManager should accept escalation settings."""
        from baldur_pro.services.runtime_config import (
            RuntimeConfigManager,
            reset_runtime_config_manager,
        )

        reset_runtime_config_manager()
        manager = RuntimeConfigManager()

        result = manager.update_error_budget_config(
            escalation_enabled=True,
            escalation_channel="#ops-escalation",
            escalation_mention="@oncall",
        )

        assert result["escalation_enabled"] is True
        assert result["escalation_channel"] == "#ops-escalation"
        assert result["escalation_mention"] == "@oncall"


# =============================================================================
# Serializer Tests - MOVED to tests/integration/django/test_serializers.py
# =============================================================================
# Serializer tests require Django + REST Framework configured.
# They are now in: tests/integration/django/test_serializers.py
# =============================================================================
