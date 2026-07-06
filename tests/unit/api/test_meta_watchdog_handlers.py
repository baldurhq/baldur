"""Unit tests for the meta-watchdog escalation self-test handler (impl 569).

Target: ``baldur.api.handlers.meta_watchdog.meta_watchdog_send_test`` — the
framework-agnostic operator self-test action shared by the admin route and the
``baldur escalation test`` CLI command.

Verification techniques applied (§8):
  - §8.8 State transition / branch — EscalationResult outcome -> HTTP status
  - §8.2 Exception/edge cases — unexpected error -> 500
  - §8.1 Boundary — the handler does NOT gate on settings.enabled
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from baldur.api.handlers.meta_watchdog import meta_watchdog_send_test
from baldur.interfaces.web_framework import HttpMethod, RequestContext
from baldur.meta.config import MetaWatchdogSettings
from baldur.meta.escalation import EscalationManager, EscalationResult


def _make_ctx(method: str = "POST", path: str = "/meta-watchdog/escalation-test"):
    return RequestContext(method=HttpMethod(method), path=path)


def _patch_manager_returning(result: EscalationResult):
    """Patch the lazily-imported EscalationManager so send_test() yields result."""
    instance = MagicMock(spec=EscalationManager)
    instance.send_test.return_value = result
    return patch("baldur.meta.escalation.EscalationManager", return_value=instance)


class TestMetaWatchdogSendTestHandler:
    """meta_watchdog_send_test() — EscalationResult -> HTTP status mapping (D5)."""

    @pytest.mark.parametrize(
        ("result", "expected_status"),
        [
            (
                EscalationResult(
                    success=True, channels_sent=["slack"], channels_failed=[]
                ),
                200,
            ),
            (
                EscalationResult(
                    success=False,
                    channels_sent=[],
                    channels_failed=[],
                    error_message="No escalation channel configured",
                ),
                400,
            ),
            (
                EscalationResult(
                    success=False,
                    channels_sent=[],
                    channels_failed=["slack"],
                    error_message="slack: HTTP 403",
                ),
                502,
            ),
            (
                EscalationResult(
                    success=False,
                    channels_sent=["slack"],
                    channels_failed=["pagerduty"],
                    error_message="pagerduty: boom",
                ),
                502,
            ),
        ],
        ids=[
            "all_delivered_200",
            "none_configured_400",
            "all_failed_502",
            "partial_failed_502",
        ],
    )
    def test_send_test_handler_maps_outcome_to_http_status(
        self, result, expected_status
    ):
        """Each self-test outcome maps to its designed HTTP status."""
        with _patch_manager_returning(result):
            resp = meta_watchdog_send_test(_make_ctx())

        assert resp.status_code == expected_status

    def test_send_test_handler_body_carries_channel_lists(self):
        """The body always carries success/channels/error_message (not the
        bad_request/server_error shape) so the CLI can parse channels."""
        result = EscalationResult(
            success=False,
            channels_sent=["slack"],
            channels_failed=["pagerduty"],
            error_message="pagerduty: boom",
        )
        with _patch_manager_returning(result):
            resp = meta_watchdog_send_test(_make_ctx())

        assert resp.body["success"] is False
        assert resp.body["channels_sent"] == ["slack"]
        assert resp.body["channels_failed"] == ["pagerduty"]
        assert resp.body["error_message"] == "pagerduty: boom"

    def test_send_test_handler_does_not_gate_on_settings_enabled(self):
        """A self-test still runs when the watchdog loop is disabled (D4).

        Unlike the sibling liveness/status/force_check handlers, send_test does
        not short-circuit on ``enabled=False`` — validating a webhook before
        enabling the watchdog is a primary use case.
        """
        # Given a disabled watchdog but a configured, delivering Slack channel
        settings = MetaWatchdogSettings(
            enabled=False,
            escalation_enabled=False,
            slack_webhook_url="https://hooks.slack.com/test",
        )
        real_manager = EscalationManager(settings=settings)

        # Delivery goes through the notification seam; inject a fake slack
        # adapter so the real send_test() delivers deterministically.
        from baldur.factory import ProviderRegistry
        from baldur.interfaces.notification import (
            NotificationAdapter,
            NotificationChannel,
        )

        class _FakeSlack(NotificationAdapter):
            def send(self, payload):
                return True

            def send_batch(self, payloads):
                return len(payloads)

            @property
            def channel(self):
                return NotificationChannel.SLACK

        snapshot = ProviderRegistry.notification.save_state()
        fake = _FakeSlack()
        ProviderRegistry.register_notification("slack", lambda: fake)
        ProviderRegistry.notification.set_instance("slack", fake)
        try:
            # When the handler runs
            with patch(
                "baldur.meta.escalation.EscalationManager",
                return_value=real_manager,
            ):
                resp = meta_watchdog_send_test(_make_ctx())
        finally:
            ProviderRegistry.notification.restore_state(snapshot)

        # Then it delivers — no "disabled" short-circuit
        assert resp.status_code == 200
        assert resp.body["channels_sent"] == ["slack"]

    def test_send_test_handler_unexpected_error_maps_to_500(self):
        """An unexpected exception is converted to a 500 server error."""
        instance = MagicMock(spec=EscalationManager)
        instance.send_test.side_effect = RuntimeError("boom")
        with patch("baldur.meta.escalation.EscalationManager", return_value=instance):
            resp = meta_watchdog_send_test(_make_ctx())

        assert resp.status_code == 500
