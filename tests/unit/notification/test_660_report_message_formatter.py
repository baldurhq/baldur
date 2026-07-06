"""
Unit tests for #660 — ``format_report_message`` (G1/G2/D2).

Target: ``baldur.services.security_notification.formatters.format_report_message``
— the OSS pure formatter that produces the same channel-agnostic dict shape as
``format_alert_message`` **except** the ``description`` carries the full,
untruncated report body (a multi-section digest must not be cut at the
alert-sized ``description_max_length`` = 500 bound) and the dict carries an
``is_report`` marker routing the Slack handler to the chunked report path.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from baldur.services.security_notification.formatters import (
    format_alert_message,
    format_report_message,
)
from baldur.settings.notification import (
    get_notification_settings,
    reset_notification_settings,
)

_LIMITS_TARGET = "baldur.services.security_notification.models._get_notification_limits"


def _render(
    title: str = "Autonomous Daily Report",
    message: str = "body",
    severity: str = "info",
    metadata: dict | None = None,
    *,
    title_limit: int = 150,
    desc_limit: int = 500,
) -> dict:
    """Render with hermetic limits so boundaries don't depend on settings state."""
    limits = MagicMock(title_max_length=title_limit, description_max_length=desc_limit)
    with patch(_LIMITS_TARGET, return_value=limits):
        return format_report_message(title, message, severity, metadata)


# =============================================================================
# Contract Tests — dict shape + design-doc default bounds
# =============================================================================


class TestFormatReportMessageContract:
    """Pin the dict shape, the ``is_report`` marker, and the documented bounds."""

    def test_default_title_limit_is_150(self):
        """Design-doc D2: title keeps the 150-char alert title bound."""
        reset_notification_settings()
        assert get_notification_settings().title_max_length == 150

    def test_default_description_limit_is_500(self):
        """Design-doc D2: 500 is the *alert* bound the report body must bypass."""
        reset_notification_settings()
        assert get_notification_settings().description_max_length == 500

    def test_shape_parity_with_alert_plus_is_report(self):
        """Report dict keys == alert dict keys plus exactly ``is_report``."""
        limits = MagicMock(title_max_length=150, description_max_length=500)
        with patch(_LIMITS_TARGET, return_value=limits):
            alert = format_alert_message("t", "m", "info", {"k": "v"})
            report = format_report_message("t", "m", "info", {"k": "v"})

        assert set(report) - set(alert) == {"is_report"}
        assert set(alert) - set(report) == set()

    def test_is_report_marker_is_true(self):
        """The ``is_report`` marker is present and truthy."""
        result = _render()

        assert result["is_report"] is True


# =============================================================================
# Behavior Tests — no-truncation, title bound, metadata, severity
# =============================================================================


class TestFormatReportMessageBehavior:
    """The body bypasses the 500 bound; the title stays bounded."""

    def test_body_over_limit_is_not_truncated(self):
        """A > description_max_length body survives verbatim (G1)."""
        body = "X" * 600

        result = _render(message=body, desc_limit=500)

        assert result["description"] == body
        assert len(result["description"]) > 500

    def test_body_far_over_limit_preserved_verbatim(self):
        """A realistic multi-section digest (thousands of chars) is untouched."""
        body = "\n".join(f"* section line {i}" for i in range(400))

        result = _render(message=body, desc_limit=500)

        assert result["description"] == body

    def test_title_truncated_to_title_max_length(self):
        """The title still respects ``title_max_length`` (reports' titles are short)."""
        long_title = "T" * 300

        result = _render(title=long_title, title_limit=150)

        assert len(result["title"]) == 150
        assert result["title"].endswith("...")

    def test_title_within_limit_not_truncated(self):
        result = _render(title="Daily Report", title_limit=150)

        assert result["title"] == "Daily Report"

    def test_metadata_retained_for_pagerduty_custom_details(self):
        """Metadata is kept populated (D3) so PagerDuty custom_details is unchanged."""
        meta = {"category": "report", "dlq_pending_count": 5, "source": "daily_report"}

        result = _render(metadata=meta)

        assert result["metadata"] == meta

    def test_metadata_none_becomes_empty_dict(self):
        result = _render(metadata=None)

        assert result["metadata"] == {}

    def test_severity_uppercased(self):
        result = _render(severity="info")

        assert result["severity"] == "INFO"

    def test_detected_at_is_iso_timestamp(self):
        result = _render()

        # isoformat() always carries the date/time 'T' separator.
        assert isinstance(result["detected_at"], str)
        assert "T" in result["detected_at"]
