"""
Actionable Alert Tests.

Tests for:
1. ActionableAlertUrlBuilder - URL building logic
2. CB notification handler Celery delegation with actionable URLs
3. Behavior under different environment variable configurations

Slack rendering of these URLs is owned by the single metadata-aware
renderer (SlackHandlerMixin._format_slack_alert) — see
tests/pro/unit/services/security_notification/test_slack_actionable_rendering.py.
"""

import os
from unittest.mock import MagicMock, patch

import pytest


class TestActionableAlertUrlBuilder:
    """ActionableAlertUrlBuilder class tests."""

    def setup_method(self):
        """Reset env vars and the singleton before each test."""
        from baldur.services.circuit_breaker.actionable_alert_urls import (
            reset_actionable_alert_url_builder,
        )

        reset_actionable_alert_url_builder()
        # Back up env vars
        self._env_backup = {
            "CB_DASHBOARD_URL": os.environ.get("CB_DASHBOARD_URL"),
            "CB_ADMIN_BASE_URL": os.environ.get("CB_ADMIN_BASE_URL"),
            "CB_RUNBOOK_URL": os.environ.get("CB_RUNBOOK_URL"),
        }

    def teardown_method(self):
        """Restore env vars and reset the singleton after each test."""
        from baldur.services.circuit_breaker.actionable_alert_urls import (
            reset_actionable_alert_url_builder,
        )

        reset_actionable_alert_url_builder()
        # Restore env vars
        for key, value in self._env_backup.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_builder_exists(self):
        """ActionableAlertUrlBuilder class exists."""
        from baldur.services.circuit_breaker.actionable_alert_urls import (
            ActionableAlertUrlBuilder,
            get_actionable_alert_url_builder,
        )

        builder = get_actionable_alert_url_builder()
        assert isinstance(builder, ActionableAlertUrlBuilder)

    def test_build_cb_open_urls_with_all_env_vars(self):
        """URL building with all env vars set."""
        from baldur.services.circuit_breaker.actionable_alert_urls import (
            get_actionable_alert_url_builder,
            reset_actionable_alert_url_builder,
        )

        os.environ["CB_DASHBOARD_URL"] = "https://grafana.internal/d/circuit-breaker"
        os.environ["CB_ADMIN_BASE_URL"] = "/admin/baldur/circuitbreaker/"
        os.environ["CB_RUNBOOK_URL"] = (
            "https://docs.internal/runbooks/circuit-breaker-recovery"
        )
        reset_actionable_alert_url_builder()

        builder = get_actionable_alert_url_builder()
        urls = builder.build_cb_open_urls(
            service_name="payment_service",
            trigger_time="2026-01-06T10:00:00Z",
        )

        # Dashboard URL
        assert urls.dashboard_url is not None
        assert "payment_service" in urls.dashboard_url
        assert "grafana.internal" in urls.dashboard_url

        # Admin URL
        assert urls.admin_url is not None
        assert "service_id=payment_service" in urls.admin_url
        assert "action=review" in urls.admin_url
        assert "trigger_time=2026-01-06T10" in urls.admin_url

        # Runbook URL
        assert urls.runbook_url is not None
        assert "circuit-breaker-recovery" in urls.runbook_url

    def test_build_cb_open_urls_without_env_vars(self):
        """Returns None for every URL when no env vars are set."""
        from baldur.services.circuit_breaker.actionable_alert_urls import (
            get_actionable_alert_url_builder,
            reset_actionable_alert_url_builder,
        )

        os.environ.pop("CB_DASHBOARD_URL", None)
        os.environ.pop("CB_ADMIN_BASE_URL", None)
        os.environ.pop("CB_RUNBOOK_URL", None)
        reset_actionable_alert_url_builder()

        builder = get_actionable_alert_url_builder()
        urls = builder.build_cb_open_urls(service_name="test_service")

        assert urls.dashboard_url is None
        assert urls.admin_url is None
        assert urls.runbook_url is None
        assert not urls.has_any_url()

    def test_dashboard_url_with_query_param_separator(self):
        """Uses & when the dashboard URL already contains ?."""
        from baldur.services.circuit_breaker.actionable_alert_urls import (
            get_actionable_alert_url_builder,
            reset_actionable_alert_url_builder,
        )

        os.environ["CB_DASHBOARD_URL"] = "https://grafana.internal/d/cb?orgId=1"
        reset_actionable_alert_url_builder()

        builder = get_actionable_alert_url_builder()
        urls = builder.build_cb_open_urls(service_name="order_service")

        assert urls.dashboard_url is not None
        assert "orgId=1&service=order_service" in urls.dashboard_url

    def test_admin_url_query_params(self):
        """Admin URL query parameter format."""
        from baldur.services.circuit_breaker.actionable_alert_urls import (
            get_actionable_alert_url_builder,
            reset_actionable_alert_url_builder,
        )

        os.environ["CB_ADMIN_BASE_URL"] = "/admin/baldur/circuitbreaker"
        reset_actionable_alert_url_builder()

        builder = get_actionable_alert_url_builder()
        urls = builder.build_cb_open_urls(
            service_name="inventory_service",
            trigger_time="2026-01-06T12:00:00Z",
        )

        assert urls.admin_url is not None
        # URL-encoded format
        assert "service_id=inventory_service" in urls.admin_url
        assert "action=review" in urls.admin_url

    def test_build_cb_closed_urls(self):
        """URL building for a CB CLOSED event."""
        from baldur.services.circuit_breaker.actionable_alert_urls import (
            get_actionable_alert_url_builder,
            reset_actionable_alert_url_builder,
        )

        os.environ["CB_DASHBOARD_URL"] = "https://grafana.internal/d/cb"
        os.environ["CB_ADMIN_BASE_URL"] = "/admin/baldur/circuitbreaker/"
        os.environ["CB_RUNBOOK_URL"] = "https://docs.internal/runbooks/cb"
        reset_actionable_alert_url_builder()

        builder = get_actionable_alert_url_builder()
        urls = builder.build_cb_closed_urls(
            service_name="user_service",
            recovery_time="2026-01-06T14:00:00Z",
        )

        assert urls.dashboard_url is not None
        assert urls.admin_url is not None
        assert "action=history" in urls.admin_url
        # No runbook needed on recovery
        assert urls.runbook_url is None

    def test_build_governance_blocked_urls(self):
        """URL building for a Governance Blocked event."""
        from baldur.services.circuit_breaker.actionable_alert_urls import (
            get_actionable_alert_url_builder,
            reset_actionable_alert_url_builder,
        )

        os.environ["CB_DASHBOARD_URL"] = "https://grafana.internal/d/cb"
        os.environ["CB_ADMIN_BASE_URL"] = "/admin/baldur/circuitbreaker/"
        os.environ["CB_RUNBOOK_URL"] = "https://docs.internal/runbooks/cb"
        reset_actionable_alert_url_builder()

        builder = get_actionable_alert_url_builder()
        urls = builder.build_governance_blocked_urls(
            service_name="blocked_service",
            reason="blast_radius_exceeded",
        )

        assert urls.dashboard_url is not None
        assert urls.admin_url is not None
        assert "action=governance_review" in urls.admin_url
        # Jumps to the governance section
        assert urls.runbook_url is not None
        assert "#governance" in urls.runbook_url

    def test_is_configured(self):
        """Env var configuration status check."""
        from baldur.services.circuit_breaker.actionable_alert_urls import (
            get_actionable_alert_url_builder,
            reset_actionable_alert_url_builder,
        )

        # All unset
        os.environ.pop("CB_DASHBOARD_URL", None)
        os.environ.pop("CB_ADMIN_BASE_URL", None)
        os.environ.pop("CB_RUNBOOK_URL", None)
        reset_actionable_alert_url_builder()

        builder = get_actionable_alert_url_builder()
        assert not builder.is_configured()

        # Only one set
        os.environ["CB_DASHBOARD_URL"] = "https://grafana.internal"
        reset_actionable_alert_url_builder()

        builder = get_actionable_alert_url_builder()
        assert builder.is_configured()

    def test_get_config_status(self):
        """Configuration status dictionary."""
        from baldur.services.circuit_breaker.actionable_alert_urls import (
            get_actionable_alert_url_builder,
            reset_actionable_alert_url_builder,
        )

        os.environ["CB_DASHBOARD_URL"] = "https://grafana.internal"
        os.environ.pop("CB_ADMIN_BASE_URL", None)
        os.environ["CB_RUNBOOK_URL"] = "https://docs.internal"
        reset_actionable_alert_url_builder()

        builder = get_actionable_alert_url_builder()
        status = builder.get_config_status()

        assert status["dashboard_configured"] is True
        assert status["admin_configured"] is False
        assert status["runbook_configured"] is True


class TestActionableUrls:
    """ActionableUrls dataclass tests."""

    def test_to_dict(self):
        """to_dict method."""
        from baldur.services.circuit_breaker.actionable_alert_urls import (
            ActionableUrls,
        )

        urls = ActionableUrls(
            dashboard_url="https://dashboard.test",
            admin_url="/admin/test",
            runbook_url="https://runbook.test",
        )

        result = urls.to_dict()

        assert result["dashboard_url"] == "https://dashboard.test"
        assert result["admin_url"] == "/admin/test"
        assert result["runbook_url"] == "https://runbook.test"

    def test_has_any_url_true(self):
        """has_any_url is True when any URL is set."""
        from baldur.services.circuit_breaker.actionable_alert_urls import (
            ActionableUrls,
        )

        urls = ActionableUrls(dashboard_url="https://test.com")
        assert urls.has_any_url() is True

    def test_has_any_url_false(self):
        """has_any_url is False when no URL is set."""
        from baldur.services.circuit_breaker.actionable_alert_urls import (
            ActionableUrls,
        )

        urls = ActionableUrls()
        assert urls.has_any_url() is False


class TestCBNotificationHandlerActionableUrls:
    """CB notification handler actionable URL delegation tests."""

    def setup_method(self):
        """Reset env vars and the event bus before each test."""
        from baldur.services.circuit_breaker.actionable_alert_urls import (
            reset_actionable_alert_url_builder,
        )
        from baldur.services.event_bus import get_event_bus

        reset_actionable_alert_url_builder()
        self.bus = get_event_bus()
        self.bus.reset()

        self._env_backup = {
            "CB_DASHBOARD_URL": os.environ.get("CB_DASHBOARD_URL"),
            "CB_ADMIN_BASE_URL": os.environ.get("CB_ADMIN_BASE_URL"),
            "CB_RUNBOOK_URL": os.environ.get("CB_RUNBOOK_URL"),
        }

    def teardown_method(self):
        """Restore env vars after each test."""
        from baldur.services.circuit_breaker.actionable_alert_urls import (
            reset_actionable_alert_url_builder,
        )

        reset_actionable_alert_url_builder()
        self.bus.reset()

        for key, value in self._env_backup.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    @patch("baldur.adapters.celery.tasks.send_cb_open_notification.delay")
    def test_notification_delegates_to_celery_with_service_name(self, mock_delay):
        """The handler delegates to the Celery task forwarding service_name."""
        from baldur.services.circuit_breaker.actionable_alert_urls import (
            reset_actionable_alert_url_builder,
        )
        from baldur.services.event_bus import (
            BaldurEvent,
            EventType,
            _on_circuit_breaker_opened_notify,
        )

        os.environ["CB_DASHBOARD_URL"] = "https://grafana.test/d/cb"
        os.environ["CB_ADMIN_BASE_URL"] = "/admin/cb/"
        os.environ["CB_RUNBOOK_URL"] = "https://docs.test/runbook"
        reset_actionable_alert_url_builder()

        event = BaldurEvent(
            event_type=EventType.CIRCUIT_BREAKER_OPENED,
            data={
                "service_name": "payment_service",
                "timestamp": "2026-01-06T10:00:00Z",
            },
            source="test",
        )

        _on_circuit_breaker_opened_notify(event)

        # service_name forwarded verbatim to the Celery task
        mock_delay.assert_called_once()
        call_kwargs = mock_delay.call_args[1]
        assert call_kwargs["service_name"] == "payment_service"
        assert call_kwargs["timestamp"] == "2026-01-06T10:00:00Z"

    @patch("baldur.adapters.celery.tasks.send_cb_open_notification.delay")
    def test_notification_delegates_without_env_vars(self, mock_delay):
        """Celery delegation works even without the CB_* env vars."""
        from baldur.services.circuit_breaker.actionable_alert_urls import (
            reset_actionable_alert_url_builder,
        )
        from baldur.services.event_bus import (
            BaldurEvent,
            EventType,
            _on_circuit_breaker_opened_notify,
        )

        os.environ.pop("CB_DASHBOARD_URL", None)
        os.environ.pop("CB_ADMIN_BASE_URL", None)
        os.environ.pop("CB_RUNBOOK_URL", None)
        reset_actionable_alert_url_builder()

        event = BaldurEvent(
            event_type=EventType.CIRCUIT_BREAKER_OPENED,
            data={"service_name": "test_service"},
            source="test",
        )

        _on_circuit_breaker_opened_notify(event)

        mock_delay.assert_called_once()
        call_kwargs = mock_delay.call_args[1]
        assert call_kwargs["service_name"] == "test_service"


class TestSendCbCloseNotificationBehavior:
    """send_cb_close_notification task payload tests (612 D4/D5).

    The task is the CB-recovery (stand-down) mirror of
    send_cb_open_notification. It builds a Slack-only, low-urgency resolve
    payload via the singleton UnifiedNotificationManager and the existing
    build_cb_closed_urls deep links. baldur_pro is stubbed at the module
    level so the payload kwargs can be captured without a real transport.
    """

    def setup_method(self):
        """Reset the URL builder singleton and back up the CB_* env vars."""
        from baldur.services.circuit_breaker.actionable_alert_urls import (
            reset_actionable_alert_url_builder,
        )

        reset_actionable_alert_url_builder()
        self._env_backup = {
            "CB_DASHBOARD_URL": os.environ.get("CB_DASHBOARD_URL"),
            "CB_ADMIN_BASE_URL": os.environ.get("CB_ADMIN_BASE_URL"),
            "CB_RUNBOOK_URL": os.environ.get("CB_RUNBOOK_URL"),
        }

    def teardown_method(self):
        """Restore env vars and reset the URL builder singleton."""
        from baldur.services.circuit_breaker.actionable_alert_urls import (
            reset_actionable_alert_url_builder,
        )

        reset_actionable_alert_url_builder()
        for key, value in self._env_backup.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _run_task_capturing_payload(self, **task_kwargs):
        """Run the task with baldur_pro stubbed, capturing NotificationPayload kwargs.

        Returns ``(result, captured_payload_kwargs, mock_pro_module)``.
        """
        captured: dict = {}
        mock_pro = MagicMock()
        mock_pro.NotificationPayload.side_effect = lambda **kwargs: (
            captured.update(kwargs) or MagicMock()
        )

        from baldur.celery_tasks.circuit_breaker_tasks import (
            send_cb_close_notification,
        )

        with patch.dict(
            "sys.modules",
            {"baldur_pro.services.unified_notification": mock_pro},
        ):
            result = send_cb_close_notification(**task_kwargs)

        return result, captured, mock_pro

    def test_close_payload_is_low_priority_slack_only_with_dedup_key(self):
        """Resolve payload pins LOW + channels=['slack'] + CB category + dedup_key."""
        # Given env URLs configured so the metadata carries real deep links
        os.environ["CB_DASHBOARD_URL"] = "https://grafana.test/d/cb"
        os.environ["CB_ADMIN_BASE_URL"] = "/admin/cb/"
        os.environ["CB_RUNBOOK_URL"] = "https://docs.test/runbook"
        from baldur.services.circuit_breaker.actionable_alert_urls import (
            reset_actionable_alert_url_builder,
        )

        reset_actionable_alert_url_builder()

        # When the resolve notification task runs for a recovered service
        result, payload, mock_pro = self._run_task_capturing_payload(
            service_name="payment_service",
            timestamp="2026-01-06T14:00:00Z",
            previous_state="open",
            trigger="auto",
        )

        # Then the payload is the Slack-only low-urgency resolve contract
        assert result["success"] is True
        assert payload["priority"] is mock_pro.NotificationPriority.LOW
        assert payload["category"] is mock_pro.NotificationCategory.CIRCUIT_BREAKER
        assert payload["channels"] == ["slack"]
        assert payload["dedup_key"] == "cb:payment_service:resolved"
        assert payload["source"] == "circuit_breaker_service"
        assert "payment_service" in payload["title"]

    def test_close_metadata_carries_event_context_and_trigger(self):
        """Metadata pins event_type, trigger_time, previous_state and trigger."""
        result, payload, _ = self._run_task_capturing_payload(
            service_name="order_service",
            timestamp="2026-01-06T15:00:00Z",
            previous_state="half_open",
            trigger="manual",
        )

        assert result["success"] is True
        metadata = payload["metadata"]
        assert metadata["service_name"] == "order_service"
        assert metadata["event_type"] == "circuit_breaker_closed"
        assert metadata["trigger_time"] == "2026-01-06T15:00:00Z"
        assert metadata["previous_state"] == "half_open"
        assert metadata["trigger"] == "manual"

    def test_close_without_env_vars_yields_none_url_metadata(self):
        """Unset CB_* base URLs produce None metadata values (tolerated downstream)."""
        os.environ.pop("CB_DASHBOARD_URL", None)
        os.environ.pop("CB_ADMIN_BASE_URL", None)
        os.environ.pop("CB_RUNBOOK_URL", None)
        from baldur.services.circuit_breaker.actionable_alert_urls import (
            reset_actionable_alert_url_builder,
        )

        reset_actionable_alert_url_builder()

        _, payload, _ = self._run_task_capturing_payload(
            service_name="user_service",
            timestamp="2026-01-06T16:00:00Z",
        )

        metadata = payload["metadata"]
        assert metadata["dashboard_url"] is None
        assert metadata["admin_url"] is None
        assert metadata["runbook_url"] is None

    def test_close_urls_are_history_links_without_runbook(self):
        """build_cb_closed_urls supplies a history admin link and no runbook button."""
        os.environ["CB_DASHBOARD_URL"] = "https://grafana.test/d/cb"
        os.environ["CB_ADMIN_BASE_URL"] = "/admin/cb/"
        os.environ["CB_RUNBOOK_URL"] = "https://docs.test/runbook"
        from baldur.services.circuit_breaker.actionable_alert_urls import (
            reset_actionable_alert_url_builder,
        )

        reset_actionable_alert_url_builder()

        _, payload, _ = self._run_task_capturing_payload(
            service_name="payment_service",
            timestamp="2026-01-06T17:00:00Z",
        )

        metadata = payload["metadata"]
        assert metadata["dashboard_url"] is not None
        assert metadata["admin_url"] is not None
        assert "action=history" in metadata["admin_url"]
        # Recovery does not warrant a runbook action — None even with env set.
        assert metadata["runbook_url"] is None

    @pytest.mark.parametrize(
        "trigger",
        ["auto", "manual", "manual_reset"],
        ids=["auto", "manual", "manual_reset"],
    )
    def test_close_message_states_trigger_for_every_recovery_kind(self, trigger):
        """Every recovery trigger sends and states the trigger in the message."""
        result, payload, _ = self._run_task_capturing_payload(
            service_name="inventory_service",
            timestamp="2026-01-06T18:00:00Z",
            previous_state="open",
            trigger=trigger,
        )

        assert result["success"] is True
        assert trigger in payload["message"]

    def test_close_pro_unavailable_falls_back_to_oss_seam(self):
        """ImportError on baldur_pro routes the recovery notice to OSS construction.

        Mirror of the open-task fallback (640): constructs the Slack webhook
        adapter from MetaWatchdogSettings.slack_webhook_url (outside the registry
        seam) and POSTs — no raise, no legacy ``baldur_pro_unavailable`` skip.
        """
        from types import SimpleNamespace

        from baldur.celery_tasks.circuit_breaker_tasks import (
            send_cb_close_notification,
        )

        with (
            patch.dict(
                "sys.modules",
                {"baldur_pro.services.unified_notification": None},
            ),
            patch(
                "baldur.settings.meta_watchdog.get_meta_watchdog_settings",
                return_value=SimpleNamespace(
                    slack_webhook_url="https://hooks.slack.com/services/T/B/X"
                ),
            ),
            patch(
                "baldur.adapters.notification.webhook_adapter.safe_urlopen",
                autospec=True,
            ) as mock_urlopen,
        ):
            mock_urlopen.return_value.__enter__.return_value.status = 200
            result = send_cb_close_notification(
                service_name="payment_service",
                timestamp="2026-01-06T19:00:00Z",
                previous_state="open",
                trigger="auto",
            )

        mock_urlopen.assert_called_once()
        assert result["success"] is True
        assert result["notification_sent"] is True
        assert result["channel"] == "slack"
        assert "reason" not in result
