"""Unit tests for the OSS Slack webhook notification adapter (639 D1-D3).

Covers :class:`SlackWebhookNotificationAdapter` — the OSS tier's single real
outbound transport (a best-effort Slack incoming-webhook POST):

- Contract: ``channel`` -> SLACK; ``timeout=None`` reads
  ``HttpClientSettings.webhook_timeout``.
- Behavior: HTTP-200 -> True / non-200 -> False / network error -> False
  (state transition + fail-open), Slack Block-Kit-lite body shape, the
  ``safe_urlopen`` mock seam, send-outcome logging (D3), and the I/O-free
  ``is_available()`` URL check.

The single mock seam is ``baldur.adapters.notification.webhook_adapter.safe_urlopen``;
no network or settings I/O happens inside ``send()`` (URL/timeout are injected).
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch
from urllib.error import URLError

import pytest
import structlog

from baldur.adapters.notification.webhook_adapter import (
    SlackWebhookNotificationAdapter,
)
from baldur.interfaces.notification import NotificationChannel
from baldur.models.notification import (
    NotificationCategory,
    NotificationPayload,
    NotificationPriority,
)
from baldur.settings.http_client import get_http_client_settings

_TEST_WEBHOOK_URL = "https://hooks.slack.com/services/T000/B000/XXX"
_SAFE_URLOPEN = "baldur.adapters.notification.webhook_adapter.safe_urlopen"


def _make_payload(
    *,
    title: str = "Circuit Breaker OPEN: payment",
    message: str = "Circuit breaker opened for service 'payment'.",
    priority: NotificationPriority = NotificationPriority.HIGH,
    source: str = "circuit_breaker_service",
) -> NotificationPayload:
    return NotificationPayload(
        title=title,
        message=message,
        priority=priority,
        category=NotificationCategory.CIRCUIT_BREAKER,
        source=source,
    )


def _set_response_status(mock_urlopen, status: int) -> None:
    """Configure an autospec'd ``safe_urlopen`` mock as a 200/non-200 CM."""
    mock_urlopen.return_value.__enter__.return_value.status = status


# =============================================================================
# Contract Tests
# =============================================================================


class TestSlackWebhookAdapterContract:
    """Hardcoded-value contracts for channel and timeout wiring."""

    def test_channel_is_slack(self):
        adapter = SlackWebhookNotificationAdapter(webhook_url=_TEST_WEBHOOK_URL)

        assert adapter.channel == NotificationChannel.SLACK

    def test_default_timeout_reads_http_client_settings(self):
        # Given a None timeout, the adapter must read the canonical
        # HttpClientSettings.webhook_timeout home (not a hardcoded literal).
        adapter = SlackWebhookNotificationAdapter(webhook_url=_TEST_WEBHOOK_URL)

        assert adapter._timeout == get_http_client_settings().webhook_timeout

    def test_explicit_timeout_overrides_settings(self):
        adapter = SlackWebhookNotificationAdapter(
            webhook_url=_TEST_WEBHOOK_URL, timeout=3.5
        )

        assert adapter._timeout == 3.5


# =============================================================================
# Behavior Tests — send()
# =============================================================================


class TestSlackWebhookAdapterBehavior:
    """send() delivery state transitions, body shape, logging, is_available."""

    def test_send_http_200_returns_true(self):
        adapter = SlackWebhookNotificationAdapter(webhook_url=_TEST_WEBHOOK_URL)

        with patch(_SAFE_URLOPEN, autospec=True) as mock_urlopen:
            _set_response_status(mock_urlopen, 200)
            result = adapter.send(_make_payload())

        assert result is True
        mock_urlopen.assert_called_once()

    def test_send_non_200_returns_false(self):
        adapter = SlackWebhookNotificationAdapter(webhook_url=_TEST_WEBHOOK_URL)

        with patch(_SAFE_URLOPEN, autospec=True) as mock_urlopen:
            _set_response_status(mock_urlopen, 500)
            result = adapter.send(_make_payload())

        assert result is False

    def test_send_fail_open_on_urlerror_returns_false(self):
        # Fail-open: a network error must surface as False, never an exception.
        adapter = SlackWebhookNotificationAdapter(webhook_url=_TEST_WEBHOOK_URL)

        with patch(_SAFE_URLOPEN, autospec=True) as mock_urlopen:
            mock_urlopen.side_effect = URLError("connection refused")
            result = adapter.send(_make_payload())

        assert result is False

    def test_send_fail_open_does_not_raise_into_caller(self):
        # The protected call must continue even if delivery raises internally.
        adapter = SlackWebhookNotificationAdapter(webhook_url=_TEST_WEBHOOK_URL)

        with patch(_SAFE_URLOPEN, autospec=True) as mock_urlopen:
            mock_urlopen.side_effect = RuntimeError("boom")
            # Must not raise.
            result = adapter.send(_make_payload())

        assert result is False

    def test_send_posts_to_configured_url(self):
        adapter = SlackWebhookNotificationAdapter(webhook_url=_TEST_WEBHOOK_URL)

        with patch(_SAFE_URLOPEN, autospec=True) as mock_urlopen:
            _set_response_status(mock_urlopen, 200)
            adapter.send(_make_payload())

        request = mock_urlopen.call_args.args[0]
        assert request.full_url == _TEST_WEBHOOK_URL
        assert request.method == "POST"

    def test_send_slack_body_shape_carries_title_message_priority(self):
        # Given a payload with a known title/message/priority/source/timestamp
        payload = _make_payload(
            title="Circuit Breaker OPEN: payment",
            message="Circuit breaker opened for service 'payment'.",
            priority=NotificationPriority.HIGH,
            source="circuit_breaker_service",
        )
        adapter = SlackWebhookNotificationAdapter(webhook_url=_TEST_WEBHOOK_URL)

        # When the body is built and POSTed
        with patch(_SAFE_URLOPEN, autospec=True) as mock_urlopen:
            _set_response_status(mock_urlopen, 200)
            adapter.send(payload)

        # Then the Slack incoming-webhook body carries the fields
        request = mock_urlopen.call_args.args[0]
        body = json.loads(request.data.decode("utf-8"))
        # Top-level text contains the title
        assert payload.title in body["text"]
        # Section block carries title + message
        blocks = body["attachments"][0]["blocks"]
        section_text = blocks[0]["text"]["text"]
        assert payload.title in section_text
        assert payload.message in section_text
        # Context block carries priority, source, and timestamp
        context_text = blocks[1]["elements"][0]["text"]
        assert payload.priority.value in context_text
        assert payload.source in context_text
        assert payload.timestamp.isoformat() in context_text

    def test_send_logging_slack_sent_on_200(self):
        adapter = SlackWebhookNotificationAdapter(webhook_url=_TEST_WEBHOOK_URL)

        with (
            patch(_SAFE_URLOPEN, autospec=True) as mock_urlopen,
            structlog.testing.capture_logs() as logs,
        ):
            _set_response_status(mock_urlopen, 200)
            adapter.send(_make_payload())

        sent = [e for e in logs if e["event"] == "notification.slack_sent"]
        assert len(sent) == 1
        assert sent[0]["log_level"] == "info"

    def test_send_logging_slack_send_failed_on_non_200(self):
        adapter = SlackWebhookNotificationAdapter(webhook_url=_TEST_WEBHOOK_URL)

        with (
            patch(_SAFE_URLOPEN, autospec=True) as mock_urlopen,
            structlog.testing.capture_logs() as logs,
        ):
            _set_response_status(mock_urlopen, 503)
            adapter.send(_make_payload())

        failed = [e for e in logs if e["event"] == "notification.slack_send_failed"]
        assert len(failed) == 1
        assert failed[0]["log_level"] == "warning"
        assert failed[0]["response_status"] == 503

    def test_send_logging_slack_send_failed_on_urlerror(self):
        adapter = SlackWebhookNotificationAdapter(webhook_url=_TEST_WEBHOOK_URL)

        with (
            patch(_SAFE_URLOPEN, autospec=True) as mock_urlopen,
            structlog.testing.capture_logs() as logs,
        ):
            mock_urlopen.side_effect = URLError("connection refused")
            adapter.send(_make_payload())

        failed = [e for e in logs if e["event"] == "notification.slack_send_failed"]
        assert len(failed) == 1
        assert failed[0]["log_level"] == "warning"

    @pytest.mark.parametrize(("status", "expected"), [(200, True), (503, False)])
    def test_send_does_not_raise_when_log_sink_cannot_encode(self, status, expected):
        # A log backend that cannot encode the (emoji) title must not defeat
        # fail-open. Both the success info-log and the failure warning-log raise
        # the encoding error; send() must still return the delivery verdict and
        # POST exactly once, so the autoretrying CB task never re-POSTs dupes.
        adapter = SlackWebhookNotificationAdapter(webhook_url=_TEST_WEBHOOK_URL)
        payload = _make_payload(title="\U0001f534 Circuit Breaker OPEN: payment")
        enc_err = UnicodeEncodeError("cp949", "\U0001f534", 0, 1, "illegal")
        mock_logger = MagicMock()
        mock_logger.info.side_effect = enc_err
        mock_logger.warning.side_effect = enc_err

        with (
            patch(_SAFE_URLOPEN, autospec=True) as mock_urlopen,
            patch("baldur.adapters.notification.webhook_adapter.logger", mock_logger),
        ):
            _set_response_status(mock_urlopen, status)
            result = adapter.send(payload)  # must not raise

        assert result is expected
        mock_urlopen.assert_called_once()

    def test_send_fail_open_when_log_sink_fails_in_except_branch(self):
        # The fail-open except branch's own logging must not re-raise: a network
        # error + an un-encodable title together must still yield False, no raise.
        adapter = SlackWebhookNotificationAdapter(webhook_url=_TEST_WEBHOOK_URL)
        payload = _make_payload(title="\U0001f534 Circuit Breaker OPEN: payment")
        mock_logger = MagicMock()
        mock_logger.warning.side_effect = UnicodeEncodeError(
            "cp949", "\U0001f534", 0, 1, "illegal"
        )

        with (
            patch(_SAFE_URLOPEN, autospec=True) as mock_urlopen,
            patch("baldur.adapters.notification.webhook_adapter.logger", mock_logger),
        ):
            mock_urlopen.side_effect = URLError("connection refused")
            result = adapter.send(payload)  # must not raise

        assert result is False

    def test_send_is_idempotent_across_repeated_calls(self):
        # Stateless adapter: N identical sends produce N POSTs, all succeeding.
        adapter = SlackWebhookNotificationAdapter(webhook_url=_TEST_WEBHOOK_URL)

        with patch(_SAFE_URLOPEN, autospec=True) as mock_urlopen:
            _set_response_status(mock_urlopen, 200)
            results = [adapter.send(_make_payload()) for _ in range(3)]

        assert results == [True, True, True]
        assert mock_urlopen.call_count == 3

    def test_send_batch_returns_success_count(self):
        adapter = SlackWebhookNotificationAdapter(webhook_url=_TEST_WEBHOOK_URL)
        payloads = [_make_payload(), _make_payload(), _make_payload()]

        with patch(_SAFE_URLOPEN, autospec=True) as mock_urlopen:
            _set_response_status(mock_urlopen, 200)
            count = adapter.send_batch(payloads)

        assert count == 3

    @pytest.mark.parametrize(
        ("priority", "expected_color"),
        [
            (NotificationPriority.CRITICAL, "#d32f2f"),
            (NotificationPriority.HIGH, "#f44336"),
            (NotificationPriority.MEDIUM, "#ff9800"),
            (NotificationPriority.LOW, "#36a64f"),
            (NotificationPriority.INFO, "#36a64f"),
        ],
    )
    def test_send_body_attachment_color_by_priority(self, priority, expected_color):
        adapter = SlackWebhookNotificationAdapter(webhook_url=_TEST_WEBHOOK_URL)

        with patch(_SAFE_URLOPEN, autospec=True) as mock_urlopen:
            _set_response_status(mock_urlopen, 200)
            adapter.send(_make_payload(priority=priority))

        body = json.loads(mock_urlopen.call_args.args[0].data.decode("utf-8"))
        assert body["attachments"][0]["color"] == expected_color

    # ---- is_available() URL-format equivalence partitioning ----

    def test_is_available_valid_https_url_returns_true(self):
        adapter = SlackWebhookNotificationAdapter(webhook_url=_TEST_WEBHOOK_URL)

        assert adapter.is_available() is True

    def test_is_available_valid_http_url_returns_true(self):
        adapter = SlackWebhookNotificationAdapter(
            webhook_url="http://internal-proxy.local/hook"
        )

        assert adapter.is_available() is True

    def test_is_available_empty_url_returns_false(self):
        adapter = SlackWebhookNotificationAdapter(webhook_url="")

        assert adapter.is_available() is False

    def test_is_available_no_scheme_returns_false(self):
        adapter = SlackWebhookNotificationAdapter(webhook_url="hooks.slack.com/x")

        assert adapter.is_available() is False

    def test_is_available_non_http_scheme_returns_false(self):
        adapter = SlackWebhookNotificationAdapter(webhook_url="ftp://hooks.slack.com/x")

        assert adapter.is_available() is False
