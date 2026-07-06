"""
Channel Routing Pipeline Integration Tests

Mock-based integration test verifying the full notification pipeline
from UnifiedNotificationManager.notify() through ChannelResolver
to formatters and SNS.deliver() handler dispatch.

Test Categories:
    A. Full Pipeline:
        - CRITICAL + OPERATIONS routes to all 4 channels
        - MEDIUM + SECURITY routes via category-first merge
        - Type filter restricts channels
        - INFO suppressed as log_only
    B. Message Formatting:
        - Formatted message reaches deliver()
    C. Cooldown:
        - Second notification suppressed by cooldown

Note: All tests use mock SNS service - no infrastructure dependency.
      This enables parallel test execution with pytest-xdist.

Reference:
    410
"""

from __future__ import annotations

import pytest

pytest.importorskip("baldur_pro")

pytestmark = pytest.mark.requires_pro


from unittest.mock import MagicMock, patch

import pytest

from baldur.models.notification import (
    NotificationCategory,
    NotificationPayload,
    NotificationPriority,
)
from baldur.services.security_notification.models import (
    ChannelDeliveryResult,
    SecurityNotificationResult,
)
from baldur.settings.channel_routing import ChannelRoutingSettings
from baldur.settings.channel_target import ChannelTargetSettings
from baldur.settings.notification import NotificationSettings
from baldur_pro.services.unified_notification.routing import ChannelResolver
from baldur_pro.services.unified_notification.service import (
    UnifiedNotificationManager,
    reset_notification_manager,
)

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture(autouse=True)
def _reset_singleton():
    """Reset UNM singleton before and after each test."""
    reset_notification_manager()
    yield
    reset_notification_manager()


def _make_resolver(
    category_slack_targets: dict[str, str] | None = None,
) -> ChannelResolver:
    """Build a ChannelResolver with real settings objects (not mocks)."""
    routing = ChannelRoutingSettings()
    if category_slack_targets:
        routing = ChannelRoutingSettings(category_slack_targets=category_slack_targets)
    notification = NotificationSettings()
    targets = ChannelTargetSettings(
        slack_webhook_url="https://hooks.slack.com/test",
        pagerduty_service_key="pd-key",
        pagerduty_enabled=True,
        dry_run=True,
    )
    return ChannelResolver(
        routing_settings=routing,
        notification_settings=notification,
        target_settings=targets,
    )


def _make_sns_service() -> MagicMock:
    """Create mock SNS service that returns success for deliver()."""
    mock_svc = MagicMock()

    def mock_deliver(formatted_message, resolved_channels):
        result = SecurityNotificationResult(incident_id=0)
        for rc in resolved_channels:
            result.add_result(ChannelDeliveryResult(channel=rc.type, success=True))
        return result

    mock_svc.deliver.side_effect = mock_deliver
    return mock_svc


# =============================================================================
# Pipeline integration: notify() → resolve → format → deliver
# =============================================================================


class TestChannelRoutingPipelineIntegration:
    """Full pipeline from UNM.notify() through to SNS.deliver()."""

    def setup_method(self):
        """Reset state before each test."""
        reset_notification_manager()

    def test_critical_operations_routes_to_slack_and_pagerduty(self):
        """
        Purpose:
            Verify CRITICAL + OPERATIONS routes through the live channel types
            and deliver() dispatches to each handler.
        Expected:
            - Result success is True
            - channels_sent contains slack, pagerduty (email/sms removed in 657)
            - deliver() called once with 2 ResolvedChannels
        """
        resolver = _make_resolver()
        manager = UnifiedNotificationManager(resolver=resolver)
        mock_svc = _make_sns_service()

        payload = NotificationPayload(
            title="Critical Alert",
            message="System down",
            priority=NotificationPriority.CRITICAL,
            category=NotificationCategory.OPERATIONS,
            source="test",
        )

        with (
            patch(
                "baldur.settings.notification.get_notification_settings",
                return_value=NotificationSettings(),
            ),
            patch(
                "baldur_pro.services.security_notification.get_security_notification_service",
                return_value=mock_svc,
            ),
        ):
            result = manager.notify(payload)

        assert result.success
        assert set(result.channels_sent) == {"slack", "pagerduty"}
        mock_svc.deliver.assert_called_once()

        # Verify resolved channels passed to deliver
        call_args = mock_svc.deliver.call_args
        resolved = call_args[0][1]  # second positional arg
        types = {rc.type for rc in resolved}
        assert types == {"slack", "pagerduty"}

    def test_medium_security_routes_slack_with_category_context(self):
        """
        Purpose:
            Verify MEDIUM + SECURITY uses category-first merge (slack only after
            657) and Slack target resolves to category_slack_targets override.
        Expected:
            - channels_sent contains slack
            - Slack target is #security-incidents (category override)
        """
        resolver = _make_resolver(
            category_slack_targets={"security": "#security-incidents"}
        )
        manager = UnifiedNotificationManager(resolver=resolver)
        mock_svc = _make_sns_service()

        payload = NotificationPayload(
            title="Security Event",
            message="Suspicious activity",
            priority=NotificationPriority.MEDIUM,
            category=NotificationCategory.SECURITY,
            source="test",
        )

        with (
            patch(
                "baldur.settings.notification.get_notification_settings",
                return_value=NotificationSettings(),
            ),
            patch(
                "baldur_pro.services.security_notification.get_security_notification_service",
                return_value=mock_svc,
            ),
        ):
            result = manager.notify(payload)

        assert result.success
        assert set(result.channels_sent) == {"slack"}

        # Verify Slack target uses category override
        call_args = mock_svc.deliver.call_args
        resolved = call_args[0][1]
        slack_channels = [rc for rc in resolved if rc.type == "slack"]
        assert slack_channels[0].target == "#security-incidents"

    def test_type_filter_restricts_channels(self):
        """
        Purpose:
            Verify payload.channels acts as type filter — CRITICAL with
            channels=["slack"] should only dispatch to Slack.
        Expected:
            - Only 1 resolved channel (slack)
            - Other CRITICAL channels (pagerduty) filtered out
        """
        resolver = _make_resolver()
        manager = UnifiedNotificationManager(resolver=resolver)
        mock_svc = _make_sns_service()

        payload = NotificationPayload(
            title="Filtered Alert",
            message="Only Slack",
            priority=NotificationPriority.CRITICAL,
            category=NotificationCategory.OPERATIONS,
            source="test",
            channels=["slack"],  # type filter
        )

        with (
            patch(
                "baldur.settings.notification.get_notification_settings",
                return_value=NotificationSettings(),
            ),
            patch(
                "baldur_pro.services.security_notification.get_security_notification_service",
                return_value=mock_svc,
            ),
        ):
            result = manager.notify(payload)

        assert result.success
        assert result.channels_sent == ["slack"]

        # Only 1 resolved channel despite CRITICAL having 4
        call_args = mock_svc.deliver.call_args
        resolved = call_args[0][1]
        assert len(resolved) == 1
        assert resolved[0].type == "slack"

    def test_info_operations_suppressed_as_log_only(self):
        """
        Purpose:
            Verify INFO + OPERATIONS resolves to empty channel list,
            causing log_only suppression.
        Expected:
            - Result is suppressed
            - suppression_reason is "log_only"
        """
        resolver = _make_resolver()
        manager = UnifiedNotificationManager(resolver=resolver)

        payload = NotificationPayload(
            title="Info Event",
            message="Just info",
            priority=NotificationPriority.INFO,
            category=NotificationCategory.OPERATIONS,
            source="test",
        )

        with patch(
            "baldur.settings.notification.get_notification_settings",
            return_value=NotificationSettings(),
        ):
            result = manager.notify(payload)

        assert result.suppressed
        assert result.suppression_reason == "log_only"

    def test_formatted_message_reaches_deliver(self):
        """
        Purpose:
            Verify format_alert_message output is passed to deliver() as
            the first positional argument with correct structure.
        Expected:
            - formatted["title"] matches payload title
            - formatted["severity"] is uppercased priority
            - formatted contains "description" and "detected_at" keys
        """
        resolver = _make_resolver()
        manager = UnifiedNotificationManager(resolver=resolver)
        mock_svc = _make_sns_service()

        payload = NotificationPayload(
            title="Format Test",
            message="Check formatting",
            priority=NotificationPriority.HIGH,
            category=NotificationCategory.OPERATIONS,
            source="test",
        )

        with (
            patch(
                "baldur.settings.notification.get_notification_settings",
                return_value=NotificationSettings(),
            ),
            patch(
                "baldur_pro.services.security_notification.get_security_notification_service",
                return_value=mock_svc,
            ),
        ):
            manager.notify(payload)

        call_args = mock_svc.deliver.call_args
        formatted = call_args[0][0]  # first positional arg
        assert formatted["title"] == "Format Test"
        assert formatted["severity"] == "HIGH"
        assert "description" in formatted
        assert "detected_at" in formatted

    def test_cooldown_suppresses_second_notification(self):
        """
        Purpose:
            Verify cooldown suppression: second notification with the same
            source+category key is suppressed within cooldown window.
        Expected:
            - First notification succeeds
            - Second notification is suppressed with reason "cooldown"
        """
        resolver = _make_resolver()
        manager = UnifiedNotificationManager(resolver=resolver)
        mock_svc = _make_sns_service()

        payload = NotificationPayload(
            title="Cooldown Test",
            message="Should be suppressed on 2nd call",
            priority=NotificationPriority.HIGH,
            category=NotificationCategory.SECURITY,
            source="cooldown_test",
        )

        with (
            patch(
                "baldur.settings.notification.get_notification_settings",
                return_value=NotificationSettings(),
            ),
            patch(
                "baldur_pro.services.security_notification.get_security_notification_service",
                return_value=mock_svc,
            ),
        ):
            first = manager.notify(payload)
            second = manager.notify(payload)

        assert first.success
        assert second.suppressed
        assert second.suppression_reason == "cooldown"


# =============================================================================
# Incident formatting pipeline: notify_incident → deliver → _send_slack
# =============================================================================


class TestIncidentFormattingPipelineIntegration:
    """Incident notifications use rich Slack formatter (_send_slack), not generic."""

    def test_incident_message_has_incident_id_at_top_level(self):
        """
        notify_incident() → _do_send() produces formatted_message with
        incident_id at the top level (not buried in metadata).

        Expected:
            - deliver() receives dict with "incident_id" key at top level
            - This enables _dispatch_slack to route to _send_slack (rich formatter)
        """
        resolver = _make_resolver()
        manager = UnifiedNotificationManager(resolver=resolver)
        mock_svc = _make_sns_service()

        with (
            patch(
                "baldur.settings.notification.get_notification_settings",
                return_value=NotificationSettings(),
            ),
            patch(
                "baldur_pro.services.security_notification.get_security_notification_service",
                return_value=mock_svc,
            ),
            patch(
                "baldur.settings.get_config",
                return_value=MagicMock(site_url="https://app.example.com"),
            ),
        ):
            from baldur_pro.services.unified_notification.convenience import (
                notify_incident,
            )

            # Bypass singleton — use our wired manager
            with patch(
                "baldur_pro.services.unified_notification.service.get_unified_notification_manager",
                return_value=manager,
            ):
                notify_incident(
                    incident_id=42,
                    incident_type="brute_force",
                    severity="high",
                    description="Repeated login failures",
                    source_ip="203.0.113.5",
                    user_id=7,
                    action_taken="IP blocked",
                )

        # Verify deliver() was called with incident_id at top level
        mock_svc.deliver.assert_called_once()
        formatted_message = mock_svc.deliver.call_args[0][0]
        assert "incident_id" in formatted_message
        assert formatted_message["incident_id"] == 42
        assert formatted_message["source_ip"] == "203.0.113.5"
        assert formatted_message["action_taken"] == "IP blocked"

    def test_dispatch_slack_routes_incident_to_send_slack(self):
        """
        _dispatch_slack routes incident messages (with incident_id) to
        _send_slack (rich formatter), not _send_slack_alert (generic).
        """
        from baldur.services.security_notification.models import (
            NotificationConfig,
        )
        from baldur_pro.services.security_notification.service import (
            SecurityNotificationService,
        )

        config = NotificationConfig(
            slack_webhook_url="https://hooks.slack.com/test",
            dry_run=True,
        )
        service = SecurityNotificationService(config=config)

        incident_message = {
            "title": "Security Incident: brute_force",
            "severity": "HIGH",
            "incident_id": 42,
            "type": "brute_force",
            "status": "open",
            "description": "Repeated login failures",
            "source_ip": "203.0.113.5",
            "user_id": 7,
            "detected_at": "2026-04-04T00:00:00Z",
            "action_taken": "IP blocked",
            "admin_url": "https://app.example.com/admin/security-incident/42/",
        }

        with patch.object(
            service,
            "_send_slack",
            return_value=ChannelDeliveryResult(channel="slack", success=True),
        ) as mock_send_slack:
            service._dispatch_slack(incident_message, "#alerts")

        mock_send_slack.assert_called_once_with(incident_message, "#alerts")

    def test_dispatch_slack_routes_alert_to_send_slack_alert(self):
        """Generic alert messages (no incident_id) use _send_slack_alert."""
        from baldur.services.security_notification.models import (
            NotificationConfig,
        )
        from baldur_pro.services.security_notification.service import (
            SecurityNotificationService,
        )

        config = NotificationConfig(
            slack_webhook_url="https://hooks.slack.com/test",
            dry_run=True,
        )
        service = SecurityNotificationService(config=config)

        alert_message = {
            "title": "SLA Drift Warning",
            "severity": "HIGH",
            "description": "Payment domain exceeded threshold",
            "detected_at": "2026-04-04T00:00:00Z",
            "metadata": {"domain": "payment"},
        }

        with patch.object(
            service,
            "_send_slack_alert",
            return_value=ChannelDeliveryResult(channel="slack", success=True),
        ) as mock_send_alert:
            service._dispatch_slack(alert_message, "#alerts")

        mock_send_alert.assert_called_once_with(alert_message, "#alerts")
