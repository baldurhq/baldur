"""The circuit-breaker push degrades to the OSS floor when the licence lapses.

A CB alert that stops arriving is worse than the OSS tier's own behaviour, so
an unentitled PRO install does not refuse this push: it takes the OSS floor,
which is the single sanctioned out-of-seam external-push exception. Two things
have to hold for that to be a real floor rather than a silent drop:

- the routing decision is made *before* the PRO import, so an unentitled worker
  never constructs the PRO notification hub; and
- the floor can reach a webhook the deployment actually configured. A PRO
  deployment configures the notification hub's Slack target, a different
  setting from the one the OSS push reads, so the caller supplies it as a
  fallback consulted only when the OSS home is unset.

Verification techniques (UNIT_TEST_GUIDELINES §8): §8.12 branch outcomes for
the routing truth table, §8.13 proximate cause (the ``fallback_webhook_url``
kwarg is reachable only from the unentitled branch, which is what separates it
from the pre-existing ImportError branch), §8.5 dependency interaction for the
delegation shape, §8.2 for the settings-fault fail-open.
"""

from __future__ import annotations

import builtins
import sys
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

import pytest

_META_WATCHDOG_SETTINGS = "baldur.settings.meta_watchdog.get_meta_watchdog_settings"
_CHANNEL_TARGET_SETTINGS = "baldur.settings.channel_target.get_channel_target_settings"
_SAFE_URLOPEN = "baldur.adapters.notification.webhook_adapter.safe_urlopen"
_VERDICT = "baldur.core.entitlement.get_entitlement_status"
_OPEN_FLOOR = "baldur.adapters.notification._send_cb_open_notification_oss"
_CLOSE_FLOOR = "baldur.adapters.notification._send_cb_close_notification_oss"
_PRO_NOTIFICATION_MODULE = "baldur_pro.services.unified_notification"

_HUB_URL = "https://hooks.slack.com/services/T000/B000/HUB"
_OSS_URL = "https://hooks.slack.com/services/T000/B000/OSS"


def _unentitled():
    """Patch the verdict getter to the MISSING answer a lapsed install gets."""
    from baldur.core.entitlement import EntitlementResult, EntitlementStatus

    return patch(
        _VERDICT,
        return_value=EntitlementResult(status=EntitlementStatus.MISSING),
    )


def _meta_watchdog_url(url):
    """Patch the OSS push's own webhook home."""
    return patch(
        _META_WATCHDOG_SETTINGS, return_value=SimpleNamespace(slack_webhook_url=url)
    )


def _channel_target_url(url):
    """Patch the PRO notification hub's webhook home."""
    return patch(
        _CHANNEL_TARGET_SETTINGS, return_value=SimpleNamespace(slack_webhook_url=url)
    )


@contextmanager
def _import_recorder():
    """Record every module name imported inside the block.

    The unentitled branch's whole point is that the PRO hub is never reached,
    and "never imported" is the observable form of that on a checkout where the
    PRO distribution is importable.
    """
    seen: list[str] = []
    real_import = builtins.__import__

    def _recording_import(name, *args, **kwargs):
        seen.append(name)
        return real_import(name, *args, **kwargs)

    with patch.object(builtins, "__import__", side_effect=_recording_import):
        yield seen


def _posted_url(mock_urlopen) -> str:
    """The URL of the single POST the Slack adapter issued."""
    mock_urlopen.assert_called_once()
    return mock_urlopen.call_args[0][0].full_url


class TestCbFloorRoutingBehavior:
    """``_cb_notification_falls_back_to_oss_floor()`` — the routing truth table.

    True only on a PRO *install* whose verdict is not ACTIVE. An OSS-only
    install answers False and reaches the floor the way it always has, through
    the ImportError branch, so its behaviour and its logs are unchanged.
    """

    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            ("ACTIVE", False),
            ("INVALID", True),
            ("MISSING", True),
        ],
        ids=["entitled", "invalid_licence", "no_licence"],
    )
    def test_verdict_decides_the_floor_on_a_pro_install(
        self, mock_pro_tier, status, expected
    ):
        """Only a non-ACTIVE verdict diverts a PRO install to the OSS floor."""
        from baldur.celery_tasks.circuit_breaker_tasks import (
            _cb_notification_falls_back_to_oss_floor,
        )
        from baldur.core.entitlement import EntitlementResult, EntitlementStatus

        with patch(
            _VERDICT,
            return_value=EntitlementResult(status=EntitlementStatus[status]),
        ):
            assert _cb_notification_falls_back_to_oss_floor() is expected

    def test_oss_only_install_does_not_take_the_entitlement_branch(self, mock_oss_tier):
        """Presence answers first: an OSS install keeps its ImportError route.

        Folding the two would change an OSS-only worker's log line and route it
        through a branch that exists for a licensing condition it cannot be in.
        """
        from baldur.celery_tasks.circuit_breaker_tasks import (
            _cb_notification_falls_back_to_oss_floor,
        )

        with patch(_VERDICT) as mock_verdict:
            assert _cb_notification_falls_back_to_oss_floor() is False

        mock_verdict.assert_not_called()


class TestCbNotificationEntitlementBehavior:
    """The two CB notification tasks under a lapsed licence."""

    def test_open_notification_delegates_to_the_floor_with_the_hub_webhook(
        self, mock_pro_tier
    ):
        """The OPEN push reaches the floor, carrying the hub's webhook home."""
        from baldur.celery_tasks.circuit_breaker_tasks import send_cb_open_notification

        floor_result = {"success": True, "channel": "slack", "notification_sent": True}
        with (
            _unentitled(),
            _channel_target_url(_HUB_URL),
            patch(_OPEN_FLOOR, return_value=floor_result) as mock_floor,
        ):
            result = send_cb_open_notification(
                service_name="payment_service",
                timestamp="2026-01-06T10:00:00Z",
            )

        mock_floor.assert_called_once_with(
            service_name="payment_service",
            timestamp="2026-01-06T10:00:00Z",
            fallback_webhook_url=_HUB_URL,
        )
        assert result is floor_result

    def test_close_notification_delegates_to_the_floor_with_recovery_context(
        self, mock_pro_tier
    ):
        """The CLOSED push forwards previous_state and trigger alongside it."""
        from baldur.celery_tasks.circuit_breaker_tasks import send_cb_close_notification

        floor_result = {"success": True, "channel": "slack", "notification_sent": True}
        with (
            _unentitled(),
            _channel_target_url(_HUB_URL),
            patch(_CLOSE_FLOOR, return_value=floor_result) as mock_floor,
        ):
            result = send_cb_close_notification(
                service_name="payment_service",
                timestamp="2026-01-06T10:05:00Z",
                previous_state="half_open",
                trigger="auto",
            )

        mock_floor.assert_called_once_with(
            service_name="payment_service",
            timestamp="2026-01-06T10:05:00Z",
            previous_state="half_open",
            trigger="auto",
            fallback_webhook_url=_HUB_URL,
        )
        assert result is floor_result

    @pytest.mark.parametrize("event", ["open", "close"], ids=["open", "close"])
    def test_unentitled_push_never_imports_the_pro_notification_hub(
        self, mock_pro_tier, event
    ):
        """The verdict is read before the PRO import, so the hub is never built.

        Asserted as "never imported" rather than "manager never constructed":
        on a checkout where the PRO distribution is importable, the import is
        the first observable step of the branch that must not run.
        """
        from baldur.celery_tasks.circuit_breaker_tasks import (
            send_cb_close_notification,
            send_cb_open_notification,
        )

        floor_result = {"success": True, "channel": "log", "notification_sent": True}
        floor_target = _OPEN_FLOOR if event == "open" else _CLOSE_FLOOR
        with (
            _unentitled(),
            _channel_target_url(""),
            patch(floor_target, return_value=floor_result),
            _import_recorder() as imported,
        ):
            if event == "open":
                send_cb_open_notification(service_name="svc", timestamp="t")
            else:
                send_cb_close_notification(
                    service_name="svc",
                    timestamp="t",
                    previous_state="open",
                    trigger="auto",
                )

        assert _PRO_NOTIFICATION_MODULE not in imported

    def test_oss_only_install_reaches_the_floor_without_a_fallback(self):
        """The pre-existing ImportError route is untouched: no fallback kwarg.

        The PRO import is failed by pinning ``None`` into ``sys.modules`` — the
        import system's own "halted" marker — rather than by relying on the
        tier of the checkout, because this file runs in both a PRO-present and
        a PRO-absent tree and only the explicit pin gives the same arm in each.
        The kwarg's absence is what distinguishes this branch from the
        entitlement one, which is otherwise indistinguishable at the floor.
        """
        from baldur.celery_tasks.circuit_breaker_tasks import send_cb_open_notification

        floor_result = {"success": True, "channel": "log", "notification_sent": True}
        with (
            patch("baldur.utils.tier.is_pro_installed", return_value=False),
            patch.dict(sys.modules, {_PRO_NOTIFICATION_MODULE: None}),
            patch(_OPEN_FLOOR, return_value=floor_result) as mock_floor,
        ):
            send_cb_open_notification(service_name="svc", timestamp="t")

        mock_floor.assert_called_once_with(service_name="svc", timestamp="t")


class TestCbFallbackWebhookBehavior:
    """``_oss_floor_fallback_webhook_url()`` — best-effort, never fatal."""

    def test_returns_the_notification_hub_webhook_home(self):
        """The fallback is the hub's Slack target, not the OSS push's own."""
        from baldur.celery_tasks.circuit_breaker_tasks import (
            _oss_floor_fallback_webhook_url,
        )

        with _channel_target_url(_HUB_URL):
            assert _oss_floor_fallback_webhook_url() == _HUB_URL

    def test_settings_fault_degrades_to_the_floors_own_resolution(self):
        """A settings fault returns "" and never raises: the task still runs,
        and the floor falls back to its own webhook home (or the logging
        adapter). Failing the task here would turn a best-effort convenience
        into a reason the alert is lost entirely."""
        from baldur.celery_tasks.circuit_breaker_tasks import (
            _oss_floor_fallback_webhook_url,
        )

        with patch(_CHANNEL_TARGET_SETTINGS, side_effect=RuntimeError("settings down")):
            assert _oss_floor_fallback_webhook_url() == ""


class TestCbDeliveryFallbackWebhookContract:
    """``_send_cb_notification_oss(fallback_webhook_url=...)`` resolution order.

    The OSS push's own home wins; the fallback is consulted only when it is
    unset; with neither set the logging adapter records intent and posts
    nothing.
    """

    @staticmethod
    def _send(*, meta_url, fallback_url, mock_urlopen_status=200):
        from baldur.adapters.notification import _send_cb_notification_oss

        kwargs = {} if fallback_url is None else {"fallback_webhook_url": fallback_url}
        with (
            _meta_watchdog_url(meta_url),
            patch(_SAFE_URLOPEN, autospec=True) as mock_urlopen,
        ):
            response = mock_urlopen.return_value.__enter__.return_value
            response.status = mock_urlopen_status
            result = _send_cb_notification_oss(
                service_name="payment_service",
                title="Circuit Breaker OPEN: payment_service",
                message="Circuit Breaker opened.",
                priority_name="high",
                event_type="circuit_breaker_opened",
                timestamp="2026-01-06T10:00:00Z",
                **kwargs,
            )
        return result, mock_urlopen

    def test_meta_watchdog_home_wins_over_the_fallback(self):
        """A configured OSS home is never overridden by the caller's fallback."""
        result, mock_urlopen = self._send(meta_url=_OSS_URL, fallback_url=_HUB_URL)

        assert _posted_url(mock_urlopen) == _OSS_URL
        assert result["channel"] == "slack"

    def test_fallback_is_used_when_the_oss_home_is_unset(self):
        """An unset OSS home hands delivery to the caller-supplied hub target."""
        result, mock_urlopen = self._send(meta_url=None, fallback_url=_HUB_URL)

        assert _posted_url(mock_urlopen) == _HUB_URL
        assert result["channel"] == "slack"

    def test_both_unset_records_intent_through_the_logging_adapter(self):
        """Neither home configured: the logging adapter, zero POSTs, no raise."""
        result, mock_urlopen = self._send(meta_url=None, fallback_url="")

        mock_urlopen.assert_not_called()
        assert result["channel"] == "log"
        assert result["notification_sent"] is True

    def test_omitting_the_argument_leaves_oss_only_resolution_unchanged(self):
        """The default is "" — an OSS-only caller resolves exactly as before.

        Same arm as passing "" explicitly: unset OSS home plus no fallback
        yields the logging adapter, so adding the parameter changed nothing for
        the callers that do not pass it.
        """
        result, mock_urlopen = self._send(meta_url=None, fallback_url=None)

        mock_urlopen.assert_not_called()
        assert result["channel"] == "log"
