"""Unit tests for 545 D6 observability — domain input rejection metric path.

Covers:
- ``DLQMetricRecorder._domain_input_rejected_total`` Counter declared with
  ``["site"]`` labels only (D6 low-cardinality contract).
- ``DLQMetricRecorder.record_domain_input_rejected(site)`` increments the
  Counter and swallows internal errors.
- ``DLQMetricEventHandler.on_domain_rejected`` parametrized over the
  3 ``site`` × 4 ``reason`` matrix — site is the only metric label, reason
  + preview live in the log payload, and the preview is sanitized.

Reference:
    545 D6
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from structlog.testing import capture_logs

from baldur.metrics.event_handlers import DLQMetricEventHandler
from baldur.metrics.recorders.dlq import DLQMetricRecorder
from baldur.utils.domain_validation import DomainRejectReason

_VALID_SITES = ["domain_context", "set_domain_context", "store_failure"]


class TestDLQRecorderDomainInputRejectedContract:
    """545 D6 recorder contract — Counter shape + name."""

    def test_counter_name_and_labels(self):
        recorder = DLQMetricRecorder()
        counter = recorder._domain_input_rejected_total
        # Prometheus client stores the full name with `_total` stripped on the
        # metric and re-added at scrape time, but the registered name keeps
        # the suffix on the underlying ``_name`` attribute.
        name_attr = getattr(counter, "_name", "")
        assert "dlq_domain_input_rejected" in name_attr

    def test_counter_increments_per_site(self):
        recorder = DLQMetricRecorder()

        # Prometheus REGISTRY is a process-singleton — capture pre-state and
        # measure deltas so test order does not pollute absolute counts.
        before_dc = recorder._domain_input_rejected_total.labels(
            site="domain_context"
        )._value.get()
        before_sf = recorder._domain_input_rejected_total.labels(
            site="store_failure"
        )._value.get()

        recorder.record_domain_input_rejected("domain_context")
        recorder.record_domain_input_rejected("domain_context")
        recorder.record_domain_input_rejected("store_failure")

        after_dc = recorder._domain_input_rejected_total.labels(
            site="domain_context"
        )._value.get()
        after_sf = recorder._domain_input_rejected_total.labels(
            site="store_failure"
        )._value.get()
        assert after_dc - before_dc == 2.0
        assert after_sf - before_sf == 1.0


class TestDLQMetricEventHandlerDomainRejectedBehavior:
    """545 D6 event handler — metric labelled by ``site`` only; log payload
    carries ``reason`` and 32-char sanitized preview."""

    @pytest.mark.parametrize("site", _VALID_SITES)
    @pytest.mark.parametrize("reason", list(DomainRejectReason))
    @patch("baldur.metrics.event_handlers._get_metrics")
    def test_metric_label_is_site_only(
        self, mock_get_metrics, site: str, reason: DomainRejectReason
    ):
        """Counter increments with only ``site`` — never ``reason`` as a label."""
        mock_metrics = MagicMock()
        mock_get_metrics.return_value = mock_metrics

        DLQMetricEventHandler.on_domain_rejected(
            site=site,
            reason=reason,
            original_domain="some_bad_value",
        )

        mock_metrics.record_dlq_domain_input_rejected.assert_called_once_with(site)

    @pytest.mark.flaky_quarantine(
        issue="545", first_seen="2026-05-28", category="mock_leak"
    )
    @pytest.mark.parametrize("site", _VALID_SITES)
    @pytest.mark.parametrize("reason", list(DomainRejectReason))
    def test_log_payload_carries_reason_value_and_preview(
        self, site: str, reason: DomainRejectReason
    ):
        with capture_logs() as logs:
            DLQMetricEventHandler.on_domain_rejected(
                site=site,
                reason=reason,
                original_domain="bad_value_for_preview",
            )

        rejected = [e for e in logs if e.get("event") == "domain.input_rejected"]
        assert len(rejected) == 1
        entry = rejected[0]
        assert entry["site"] == site
        assert entry["reason"] == reason.value
        assert "original_preview" in entry

    @pytest.mark.flaky_quarantine(
        issue="545", first_seen="2026-05-28", category="mock_leak"
    )
    @patch("baldur.metrics.event_handlers._get_metrics")
    def test_preview_truncated_to_32_chars(self, mock_get_metrics):
        """Defense-in-depth against log injection — preview is bounded."""
        mock_get_metrics.return_value = None  # metric optional

        long_input = "x" * 200
        with capture_logs() as logs:
            DLQMetricEventHandler.on_domain_rejected(
                site="store_failure",
                reason=DomainRejectReason.INVALID_CHARSET,
                original_domain=long_input,
            )

        rejected = [e for e in logs if e.get("event") == "domain.input_rejected"]
        assert len(rejected) == 1
        assert len(rejected[0]["original_preview"]) <= 32

    @pytest.mark.flaky_quarantine(
        issue="545", first_seen="2026-05-28", category="mock_leak"
    )
    @patch("baldur.metrics.event_handlers._get_metrics")
    def test_preview_sanitized_label_value(self, mock_get_metrics):
        """``sanitize_label_value`` replaces unsafe chars in the preview."""
        mock_get_metrics.return_value = None

        with capture_logs() as logs:
            DLQMetricEventHandler.on_domain_rejected(
                site="store_failure",
                reason=DomainRejectReason.INVALID_CHARSET,
                original_domain="bad name with spaces!",
            )
        rejected = [e for e in logs if e.get("event") == "domain.input_rejected"]
        preview = rejected[0]["original_preview"]
        assert " " not in preview
        assert "!" not in preview

    @pytest.mark.flaky_quarantine(
        issue="545", first_seen="2026-05-28", category="mock_leak"
    )
    @patch("baldur.metrics.event_handlers._get_metrics")
    def test_handler_handles_missing_metrics_backend(self, mock_get_metrics):
        """When the metrics backend is unavailable, log still fires."""
        mock_get_metrics.return_value = None

        with capture_logs() as logs:
            DLQMetricEventHandler.on_domain_rejected(
                site="store_failure",
                reason=DomainRejectReason.TOO_LONG,
                original_domain="x" * 100,
            )

        rejected = [e for e in logs if e.get("event") == "domain.input_rejected"]
        assert len(rejected) == 1
