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

from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from baldur.api.handlers.meta_watchdog import (
    meta_watchdog_force_check,
    meta_watchdog_send_test,
)
from baldur.interfaces.web_framework import HttpMethod, RequestContext
from baldur.meta.config import MetaWatchdogSettings
from baldur.meta.escalation import EscalationManager, EscalationResult
from baldur.meta.health_probe import HealthStatus


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


# =============================================================================
# POST /meta/force-check — 409 when a pass is already running
# =============================================================================


def _state(overall: HealthStatus = HealthStatus.HEALTHY):
    """A minimal state object with the surface _format_state() reads."""
    return SimpleNamespace(
        overall_status=overall,
        component_statuses={"redis": HealthStatus.UNHEALTHY},
        last_check=datetime(2026, 7, 28, 12, 0, tzinfo=UTC),
        escalation_count=3,
        escalation_pending=True,
        component_details={"redis": {"error": "connection refused"}},
    )


def _force_check_ctx():
    return RequestContext(method=HttpMethod("POST"), path="/meta-watchdog/force-check")


@contextmanager
def _patched_watchdog(watchdog):
    """Serve the handler a watchdog and an enabled settings object."""
    with (
        patch(
            "baldur.api.handlers.meta_watchdog._watchdog",
            return_value=watchdog,
        ),
        patch(
            "baldur.api.handlers.meta_watchdog._settings",
            return_value=MetaWatchdogSettings(enabled=True),
        ),
    ):
        yield


class TestForceCheckHandlerBehavior:
    """The force-check endpoint never queues on the watchdog's check lock."""

    def test_in_progress_check_answers_409_with_the_last_snapshot(self):
        # Given: a pass is already running, so the non-blocking entry declines
        watchdog = MagicMock()
        watchdog.try_force_check.return_value = None
        watchdog.get_state.return_value = _state(HealthStatus.UNHEALTHY)

        # When
        with _patched_watchdog(watchdog):
            resp = meta_watchdog_force_check(_force_check_ctx())

        # Then: a conflict carrying the last completed diagnosis, not an error
        assert resp.status_code == 409
        assert resp.body["error_code"] == "check_in_progress"
        assert resp.body["overall_status"] == "unhealthy"
        assert resp.body["components"] == {"redis": "unhealthy"}
        assert "already in progress" in resp.body["message"]

    def test_in_progress_check_never_falls_back_to_the_blocking_entry(self):
        # Given
        watchdog = MagicMock()
        watchdog.try_force_check.return_value = None
        watchdog.get_state.return_value = _state()

        # When
        with _patched_watchdog(watchdog):
            meta_watchdog_force_check(_force_check_ctx())

        # Then: a poller must not stack up behind the check lock — the blocking
        # entry point is the starvation path the 409 exists to avoid
        watchdog.force_check.assert_not_called()

    def test_admitted_check_returns_200_with_the_fresh_state(self):
        # Given: no other pass holds the lock
        watchdog = MagicMock()
        watchdog.try_force_check.return_value = _state(HealthStatus.DEGRADED)

        # When
        with _patched_watchdog(watchdog):
            resp = meta_watchdog_force_check(_force_check_ctx())

        # Then
        assert resp.status_code == 200
        assert resp.body["message"] == "Force check completed"
        assert resp.body["overall_status"] == "degraded"
        assert "error_code" not in resp.body

    def test_watchdog_without_the_non_blocking_entry_falls_back_to_force_check(self):
        # Given: a version-skewed install (newer OSS, older PRO watchdog)
        watchdog = SimpleNamespace(force_check=MagicMock(return_value=_state()))

        # When
        with _patched_watchdog(watchdog):
            resp = meta_watchdog_force_check(_force_check_ctx())

        # Then: the blocking path is strictly better than a 500
        assert resp.status_code == 200
        assert resp.body["message"] == "Force check completed"
        watchdog.force_check.assert_called_once_with()
