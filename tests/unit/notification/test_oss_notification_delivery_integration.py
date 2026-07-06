"""Integration tests for OSS notification delivery (640 D5 — post-reconciliation).

Mock-based (no infra) cross-component flows after doc 640 reverts 639's
notification-seam over-reach:

- ``TestCbTaskOssSeamFallback`` — the CB-task ImportError branch
  (``_send_cb_notification_oss``) constructs its own Slack transport directly
  from ``MetaWatchdogSettings.slack_webhook_url`` (no registry seam). A
  configured URL POSTs exactly once; an unset/empty URL falls back to the
  logging adapter and records ``log`` (zero POSTs).
- ``TestEscalationLogIntent`` — with nothing registered under the SLACK slot
  (the OSS reality after 640 removes the auto-registration), Meta-Watchdog
  escalation resolves the default ``LoggingNotificationAdapter`` →
  ``channels_sent == ["log"]``, no POST (push is PRO — OSS observes, PRO notifies).
- ``TestProEventExclusion`` — PRO-routed helpers (``unified_notification``) on
  OSS no-op and never POST.

Mock seams: ``baldur.adapters.notification.webhook_adapter.safe_urlopen`` for
POST capture; ``baldur.settings.meta_watchdog.get_meta_watchdog_settings`` for
the CB-task webhook URL; ``sys.modules`` for the CB-task PRO-absence branch;
``baldur.notification.helpers._get_pro`` for the PRO-event-exclusion firewall.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import URLError

import pytest

_URL = "https://hooks.slack.com/services/T000/B000/XXX"
_SAFE_URLOPEN = "baldur.adapters.notification.webhook_adapter.safe_urlopen"
_PRO_MODULE = "baldur_pro.services.unified_notification"
_CB_SETTINGS = "baldur.settings.meta_watchdog.get_meta_watchdog_settings"


@pytest.fixture(autouse=True)
def _isolate_registry():
    """Snapshot/reset/restore ProviderRegistry notification state.

    Resetting clears any PRO escalation adapters the monorepo registered into
    the ``slack`` slot, so the SLACK channel resolves to the logging fallback —
    the OSS reality this suite asserts.
    """
    from baldur.factory import ProviderRegistry

    snapshot = ProviderRegistry.notification.save_state()
    ProviderRegistry.notification.reset()
    yield
    ProviderRegistry.notification.restore_state(snapshot)


def _patch_cb_settings(slack_webhook_url):
    """Patch the CB task's webhook-URL home (MetaWatchdogSettings)."""
    return patch(
        _CB_SETTINGS,
        return_value=SimpleNamespace(slack_webhook_url=slack_webhook_url),
    )


def _set_response_status(mock_urlopen, status: int = 200) -> None:
    mock_urlopen.return_value.__enter__.return_value.status = status


# =============================================================================
# CB-task OSS direct construction (out-of-seam)
# =============================================================================


class TestCbTaskOssSeamFallback:
    """send_cb_*_notification ImportError branch -> OSS direct construction.

    Post-640 the OSS fallback constructs its own transport from
    ``MetaWatchdogSettings.slack_webhook_url`` (no registry seam): a configured
    URL builds the Slack webhook adapter and POSTs; an unset/empty URL falls
    back to the logging adapter and records ``log`` (no POST).
    """

    def test_oss_cb_open_post_once_when_url_set(self):
        # SC: forced circuit_breaker.opened on OSS-only with URL set -> exactly
        # one POST to the configured URL via the directly-constructed adapter.
        from baldur.celery_tasks.circuit_breaker_tasks import (
            send_cb_open_notification,
        )

        with (
            patch.dict("sys.modules", {_PRO_MODULE: None}),
            _patch_cb_settings(_URL),
            patch(_SAFE_URLOPEN, autospec=True) as mock_urlopen,
        ):
            _set_response_status(mock_urlopen, 200)
            result = send_cb_open_notification(
                service_name="payment_service",
                timestamp="2026-01-06T10:00:00Z",
            )

        mock_urlopen.assert_called_once()
        assert mock_urlopen.call_args.args[0].full_url == _URL
        assert result["success"] is True
        assert result["notification_sent"] is True
        assert result["channel"] == "slack"

    def test_oss_cb_close_post_once_when_url_set(self):
        # Parity: the recovery (CLOSED) task fallback also POSTs once.
        from baldur.celery_tasks.circuit_breaker_tasks import (
            send_cb_close_notification,
        )

        with (
            patch.dict("sys.modules", {_PRO_MODULE: None}),
            _patch_cb_settings(_URL),
            patch(_SAFE_URLOPEN, autospec=True) as mock_urlopen,
        ):
            _set_response_status(mock_urlopen, 200)
            result = send_cb_close_notification(
                service_name="payment_service",
                timestamp="2026-01-06T10:05:00Z",
                previous_state="open",
                trigger="auto",
            )

        mock_urlopen.assert_called_once()
        assert result["success"] is True
        assert result["channel"] == "slack"

    def test_oss_cb_open_url_unset_logs_no_post(self):
        # URL unset -> logging fallback: records ``log``, zero POSTs, no raise.
        from baldur.celery_tasks.circuit_breaker_tasks import (
            send_cb_open_notification,
        )

        with (
            patch.dict("sys.modules", {_PRO_MODULE: None}),
            _patch_cb_settings(None),
            patch(_SAFE_URLOPEN, autospec=True) as mock_urlopen,
        ):
            result = send_cb_open_notification(
                service_name="payment_service",
                timestamp="2026-01-06T10:00:00Z",
            )

        mock_urlopen.assert_not_called()
        assert result["channel"] == "log"
        # LoggingNotificationAdapter.send() returns True (logged successfully).
        assert result["notification_sent"] is True

    def test_oss_cb_empty_url_is_logging_fallback_identical_to_unset(self):
        # Empty string is falsy -> identical to unset (logging fallback, no POST).
        from baldur.adapters.notification import (
            _send_cb_notification_oss,
        )

        with (
            _patch_cb_settings(""),
            patch(_SAFE_URLOPEN, autospec=True) as mock_urlopen,
        ):
            result = _send_cb_notification_oss(
                service_name="payment_service",
                title="Circuit Breaker OPEN: payment_service",
                message="Circuit Breaker opened.",
                priority_name="high",
                event_type="circuit_breaker_opened",
                timestamp="2026-01-06T10:00:00Z",
            )

        mock_urlopen.assert_not_called()
        assert result["channel"] == "log"

    def test_oss_cb_whitespace_url_builds_adapter_fail_open_no_raise(self):
        # "   " is truthy -> the construction guard DOES build a
        # SlackWebhookNotificationAdapter -> send-time fail-open POSTs to the
        # garbage URL and logs notification.slack_send_failed, zero raise.
        from baldur.adapters.notification import (
            _send_cb_notification_oss,
        )

        with (
            _patch_cb_settings("   "),
            patch(_SAFE_URLOPEN, autospec=True) as mock_urlopen,
        ):
            mock_urlopen.side_effect = URLError("unknown url type")
            result = _send_cb_notification_oss(
                service_name="payment_service",
                title="Circuit Breaker OPEN: payment_service",
                message="Circuit Breaker opened.",
                priority_name="high",
                event_type="circuit_breaker_opened",
                timestamp="2026-01-06T10:00:00Z",
            )

        # Adapter constructed (channel slack), send failed -> not sent, no raise.
        assert result["channel"] == "slack"
        assert result["success"] is False
        assert result["notification_sent"] is False

    def test_oss_cb_fail_open_on_non_200(self):
        # A non-200 response -> send() returns False; fail-open, no raise.
        from baldur.adapters.notification import (
            _send_cb_notification_oss,
        )

        with (
            _patch_cb_settings(_URL),
            patch(_SAFE_URLOPEN, autospec=True) as mock_urlopen,
        ):
            _set_response_status(mock_urlopen, 500)
            result = _send_cb_notification_oss(
                service_name="payment_service",
                title="Circuit Breaker OPEN: payment_service",
                message="Circuit Breaker opened.",
                priority_name="high",
                event_type="circuit_breaker_opened",
                timestamp="2026-01-06T10:00:00Z",
            )

        assert result["channel"] == "slack"
        assert result["success"] is False
        assert result["notification_sent"] is False

    def test_oss_cb_post_body_carries_title(self):
        # The Slack incoming-webhook body rides the title on the top-level text.
        from baldur.adapters.notification import (
            _send_cb_notification_oss,
        )

        with (
            _patch_cb_settings(_URL),
            patch(_SAFE_URLOPEN, autospec=True) as mock_urlopen,
        ):
            _set_response_status(mock_urlopen, 200)
            _send_cb_notification_oss(
                service_name="payment_service",
                title="Circuit Breaker OPEN: payment_service",
                message="Circuit Breaker opened.",
                priority_name="high",
                event_type="circuit_breaker_opened",
                timestamp="2026-01-06T10:00:00Z",
            )

        request = mock_urlopen.call_args.args[0]
        body = json.loads(request.data.decode("utf-8"))
        assert "Circuit Breaker OPEN: payment_service" in body["text"]
        assert "high" in json.dumps(body)


# =============================================================================
# Escalation -> log intent (OSS: nothing registered under SLACK)
# =============================================================================


class TestEscalationLogIntent:
    """640: on OSS (no push adapter registered) escalation logs intent.

    With nothing registered under the SLACK slot (the OSS reality after 640
    removes the auto-registration), ``send_test`` / ``escalate`` resolve the
    default ``LoggingNotificationAdapter`` -> ``channels_sent == ["log"]``,
    zero POSTs. Live external delivery is a PRO capability (OSS observes; PRO notifies).
    """

    def _settings(self):
        from baldur.meta.config import MetaWatchdogSettings

        return MetaWatchdogSettings(
            slack_webhook_url=_URL,
            escalation_enabled=True,
            dry_run_mode=False,
        )

    def test_escalation_send_test_resolves_log_intent_no_post(self):
        from baldur.meta.escalation import EscalationManager

        manager = EscalationManager(settings=self._settings())

        with patch(_SAFE_URLOPEN, autospec=True) as mock_urlopen:
            result = manager.send_test()

        assert result.success is True
        assert result.channels_sent == ["log"]
        assert result.channels_failed == []
        mock_urlopen.assert_not_called()

    def test_escalate_warning_resolves_log_intent_no_post(self):
        from baldur.meta.escalation import (
            EscalationEvent,
            EscalationLevel,
            EscalationManager,
        )

        manager = EscalationManager(settings=self._settings())
        event = EscalationEvent(
            level=EscalationLevel.WARNING,
            title="DLQ consumer stuck",
            description="DLQ consumer stopped processing.",
            component="dlq",
        )

        with (
            patch.object(
                EscalationManager, "_acquire_cross_worker_slot", return_value=True
            ),
            patch(_SAFE_URLOPEN, autospec=True) as mock_urlopen,
        ):
            result = manager.escalate(event)

        assert result.success is True
        assert result.channels_sent == ["log"]
        mock_urlopen.assert_not_called()


# =============================================================================
# PRO-event exclusion firewall
# =============================================================================


class TestProEventExclusion:
    """PRO-routed helpers (unified_notification) no-op on OSS — zero POSTs."""

    @pytest.mark.parametrize(
        ("func_name", "call_args"),
        [
            ("notify", ("PRO title", "PRO message")),
            ("notify_security", ("Security title", "Security message")),
            ("notify_error", ("Error title", "Error message", ValueError("boom"))),
        ],
    )
    def test_pro_event_exclusion_no_post_on_oss(self, func_name, call_args):
        # A PRO-routed helper on OSS (PRO absent) no-ops and never POSTs.
        import baldur.notification.helpers as helpers

        func = getattr(helpers, func_name)

        with (
            patch.object(helpers, "_get_pro", return_value=None),
            patch(_SAFE_URLOPEN, autospec=True) as mock_urlopen,
        ):
            result = func(*call_args)

        assert result is None
        mock_urlopen.assert_not_called()
