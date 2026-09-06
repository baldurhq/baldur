"""A suppressed notification must never be logged as a delivered one.

``NotificationResult`` reports a suppression as ``success=True`` with
``suppressed=True`` — a suppression is not a delivery failure. A consumer that
branches on ``success`` alone therefore records a delivery that never left the
process, and any ``suppressed`` branch behind that ``if`` is unreachable.

Both consumers pinned here read the pair. The failure they guard against is
silent by construction: the log line is the only place the difference between
"delivered" and "withheld" is observable to an operator, so a wrong line here
is indistinguishable from a working alert path.

Verification techniques (UNIT_TEST_GUIDELINES §8): §8.12 branch outcomes over
the three result shapes (delivered / suppressed / failed), §8.4 side effects
(the emitted event name is the whole observable).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from structlog.testing import capture_logs

pytest.importorskip("baldur_pro")

pytestmark = pytest.mark.requires_pro

from baldur_pro.services.unified_notification import (
    NotificationResult,
    UnifiedNotificationManager,
)

_SUPPRESSED = NotificationResult(
    success=True, suppressed=True, suppression_reason="not_entitled"
)
_DELIVERED = NotificationResult(success=True, channels_sent=["slack"])
_FAILED = NotificationResult(success=False, error="channel unreachable")


def _events(logs) -> list[str]:
    return [entry.get("event") for entry in logs]


class TestGrafanaAlertResultLoggingBehavior:
    """``_process_single_alert`` distinguishes withheld from delivered."""

    _ALERT = {
        "status": "firing",
        "labels": {
            "alertname": "TestAlert",
            "severity": "warning",
            "category": "sla",
        },
        "annotations": {"summary": "Test Summary", "description": "Test Description"},
    }

    @classmethod
    def _process(cls, result):
        from baldur.api.handlers.grafana_webhook import _process_single_alert

        manager = MagicMock(spec=UnifiedNotificationManager)
        manager.notify.return_value = result
        with capture_logs() as logs:
            _process_single_alert(cls._ALERT, manager)
        return _events(logs)

    def test_suppressed_alert_is_not_recorded_as_sent(self):
        """The suppression branch is reached, and the sent line is not written.

        Both halves matter: asserting only the absence would pass on a handler
        that logged nothing at all.
        """
        events = self._process(_SUPPRESSED)

        assert "grafana_webhook.alert_notification_suppressed" in events
        assert "grafana_webhook.alert_notification_sent" not in events

    def test_delivered_alert_is_still_recorded_as_sent(self):
        """A real delivery keeps its own line — the guard is not a mute."""
        events = self._process(_DELIVERED)

        assert "grafana_webhook.alert_notification_sent" in events
        assert "grafana_webhook.alert_notification_suppressed" not in events

    def test_failed_alert_still_warns(self):
        """A failure is neither a delivery nor a suppression."""
        events = self._process(_FAILED)

        assert "grafana_webhook.alert_notification_failed" in events
        assert "grafana_webhook.alert_notification_sent" not in events


class TestAggregatedPostmortemResultLoggingBehavior:
    """``_send_aggregated_notification`` makes the same distinction."""

    _MANAGER_GETTER = (
        "baldur_pro.services.unified_notification.get_unified_notification_manager"
    )

    @staticmethod
    def _summary():
        return SimpleNamespace(
            total_incidents=3,
            affected_services=["payment_service", "order_service"],
            total_downtime_seconds=180,
            group_id="grp-1",
            postmortem_links=[],
            created_at="2026-01-06T10:00:00Z",
        )

    @classmethod
    def _send(cls, result):
        from baldur.adapters.celery.tasks.postmortem import (
            _send_aggregated_notification,
        )

        manager = MagicMock(spec=UnifiedNotificationManager)
        manager.notify.return_value = result
        with (
            patch(cls._MANAGER_GETTER, return_value=manager),
            capture_logs() as logs,
        ):
            # ``settings`` is accepted and never read by this helper.
            _send_aggregated_notification(cls._summary(), None)
        return _events(logs)

    def test_suppressed_summary_is_not_recorded_as_sent(self):
        """A withheld incident summary reports itself as withheld."""
        events = self._send(_SUPPRESSED)

        assert "flush_notifications.summary_notification_suppressed" in events
        assert "flush_notifications.summary_notification_sent_incidents" not in events

    def test_delivered_summary_is_still_recorded_as_sent(self):
        """The entitled path is unchanged."""
        events = self._send(_DELIVERED)

        assert "flush_notifications.summary_notification_sent_incidents" in events
        assert "flush_notifications.summary_notification_suppressed" not in events

    def test_failed_summary_still_warns(self):
        """A channel failure keeps the WARNING that names it."""
        events = self._send(_FAILED)

        assert "flush_notifications.notification_failed" in events
        assert "flush_notifications.summary_notification_sent_incidents" not in events
