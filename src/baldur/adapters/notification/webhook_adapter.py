"""Slack incoming-webhook notification adapter (OSS circuit-breaker push).

The OSS tier's single real outbound transport: a best-effort Slack
incoming-webhook POST for circuit-breaker open/close events. It is constructed
directly by the OSS circuit-breaker notification task (``baldur_pro`` absent)
from the configured webhook URL — the single sanctioned external-push exception
to the "OSS observes; PRO notifies" boundary, delivered *outside* the
notification registry seam so the seam itself stays logging-only on OSS.

Design:
- Wire format mirrors the proven PRO ``EscalationSlackAdapter`` Block-Kit
  shape, simplified — no escalation orchestration, dedup, or retry (those stay
  PRO differentiators).
- Fail-open: any non-200 / network error returns ``False`` and never raises
  into the protected call (CROSS_SERVICE_STANDARDS side-effects fail-open;
  the claim-wiring rule). Delivery outcome is logged so a silent fail-open stays diagnosable.
- URL/timeout injected via constructor (DI) — no hidden settings read inside
  ``send()``, so the adapter stays generic and unit-testable.
"""

from __future__ import annotations

import json
import urllib.request
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import structlog

from baldur.interfaces.notification import NotificationAdapter, NotificationChannel
from baldur.utils.http import safe_urlopen

if TYPE_CHECKING:
    from baldur.models.notification import NotificationPayload

logger = structlog.get_logger()

__all__ = ["SlackWebhookNotificationAdapter"]


# Slack attachment color by notification priority value.
_PRIORITY_COLOR = {
    "critical": "#d32f2f",
    "high": "#f44336",
    "medium": "#ff9800",
    "low": "#36a64f",
    "info": "#36a64f",
}
_DEFAULT_COLOR = "#808080"
_HTTP_OK = 200


class SlackWebhookNotificationAdapter(NotificationAdapter):
    """Best-effort Slack incoming-webhook push adapter (OSS outbound delivery).

    Posts a Slack Block-Kit-lite incoming-webhook body. The webhook URL and
    timeout are injected at construction so tests can point at a mock receiver
    and patch ``safe_urlopen`` without any settings read. When ``timeout`` is
    omitted it is read once from ``HttpClientSettings.webhook_timeout``.
    """

    def __init__(self, webhook_url: str, timeout: float | None = None) -> None:
        self._webhook_url = webhook_url
        if timeout is None:
            from baldur.settings.http_client import get_http_client_settings

            timeout = get_http_client_settings().webhook_timeout
        self._timeout = timeout

    def send(self, payload: NotificationPayload) -> bool:
        """POST the payload to the Slack webhook. Fail-open; never raises."""
        body = self._build_body(payload)
        try:
            data = json.dumps(body).encode("utf-8")
            request = urllib.request.Request(
                self._webhook_url,
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with safe_urlopen(request, timeout=self._timeout) as response:
                status = response.status
                if status == _HTTP_OK:
                    self._safe_log(
                        "info",
                        "notification.slack_sent",
                        title=payload.title,
                        source=payload.source,
                    )
                    return True
                self._safe_log(
                    "warning",
                    "notification.slack_send_failed",
                    title=payload.title,
                    source=payload.source,
                    response_status=status,
                )
                return False
        except Exception as e:
            # Side-effect fail-open: a non-200, scheme rejection, or network
            # error must not propagate into the protected call.
            self._safe_log(
                "warning",
                "notification.slack_send_failed",
                title=payload.title,
                source=payload.source,
                error=str(e),
            )
            return False

    @staticmethod
    def _safe_log(level: str, event: str, **fields: object) -> None:
        """Emit the delivery-outcome log — best-effort, never raises.

        The outcome log echoes ``payload.title``, which may carry non-ASCII
        characters (e.g. the circuit-breaker event emoji). A log sink that
        cannot encode the title — a non-UTF-8 console or stdlib handler — would
        otherwise raise *after* the POST already happened, escaping ``send()``
        and defeating its fail-open contract; on the autoretrying CB task that
        re-POSTs duplicates. The delivery itself is encoding-safe (the body is
        ``json.dumps`` ASCII-escaped), so a logging-backend failure must never
        propagate from here.
        """
        try:
            getattr(logger, level)(event, **fields)
        except Exception:
            pass

    def send_batch(self, payloads: list[NotificationPayload]) -> int:
        return sum(1 for p in payloads if self.send(p))

    @property
    def channel(self) -> NotificationChannel:
        return NotificationChannel.SLACK

    def is_available(self) -> bool:
        """I/O-free URL-format check (no network call).

        Returns True iff the configured webhook URL parses with an http/https
        scheme and a network location. Send-time fail-open remains the safety
        net for a URL that parses here but is unreachable at delivery time.
        """
        if not self._webhook_url:
            return False
        try:
            parts = urlsplit(self._webhook_url)
        except Exception:
            return False
        return parts.scheme in ("http", "https") and bool(parts.netloc)

    def _build_body(self, payload: NotificationPayload) -> dict:
        """Build the Slack incoming-webhook body (Block-Kit-lite).

        Generic over the payload source: the title rides the top-level ``text``
        and the section block, while priority / source / timestamp ride a
        context block. Independent of any source-specific metadata keys.
        """
        color = _PRIORITY_COLOR.get(payload.priority.value, _DEFAULT_COLOR)
        timestamp = payload.timestamp.isoformat() if payload.timestamp else ""
        return {
            "text": f"*[Baldur]* {payload.title}",
            "attachments": [
                {
                    "color": color,
                    "blocks": [
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": (
                                    f"*[Baldur]* {payload.title}\n\n{payload.message}"
                                ),
                            },
                        },
                        {
                            "type": "context",
                            "elements": [
                                {
                                    "type": "mrkdwn",
                                    "text": (
                                        f"*Priority:* {payload.priority.value} | "
                                        f"*Source:* {payload.source} | "
                                        f"*Time:* {timestamp}"
                                    ),
                                },
                            ],
                        },
                    ],
                }
            ],
        }
