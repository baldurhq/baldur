"""Unit tests for 545 chokepoints 1-3 — ``@domain_tag`` / ``DomainContext`` /
``set_domain_context`` validation behavior.

545 D4 layered split:
    - Decoration time (``@domain_tag("...")``) → raise loud at module import.
    - Runtime APIs (``DomainContext.__init__``, ``set_domain_context``) →
      fall back to ``FALLBACK_DOMAIN`` + WARNING log + metric increment.

Reference:
    545 D4, D5, D6
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from structlog.testing import capture_logs

from baldur.core.exceptions import DomainValidationError
from baldur.decorators.domain_tag import (
    DomainContext,
    clear_domain_context,
    domain_tag,
    get_current_domain,
    set_domain_context,
)
from baldur.utils.domain_validation import FALLBACK_DOMAIN, DomainRejectReason


@pytest.fixture(autouse=True)
def _clear_context_each_test():
    clear_domain_context()
    yield
    clear_domain_context()


# =============================================================================
# Chokepoint 1 — @domain_tag(...) decoration-time raise
# =============================================================================


class TestDomainTagDecorationBehavior:
    """Decoration-time validation raises ``DomainValidationError`` loud."""

    def test_decoration_raises_on_uuid(self):
        with pytest.raises(DomainValidationError) as exc_info:
            domain_tag(str(uuid4()))
        assert exc_info.value.reason is DomainRejectReason.INVALID_CHARSET

    def test_decoration_raises_on_spaces(self):
        with pytest.raises(DomainValidationError) as exc_info:
            domain_tag("invalid name with spaces!")
        assert exc_info.value.reason is DomainRejectReason.INVALID_CHARSET

    def test_decoration_raises_on_empty(self):
        with pytest.raises(DomainValidationError) as exc_info:
            domain_tag("")
        assert exc_info.value.reason is DomainRejectReason.EMPTY

    def test_decoration_raises_on_too_long(self):
        with pytest.raises(DomainValidationError) as exc_info:
            domain_tag("a" * 100)
        assert exc_info.value.reason is DomainRejectReason.TOO_LONG

    def test_decoration_accepts_valid_domain(self):
        """Valid decoration returns a decorator that does not raise."""

        @domain_tag("payment.charge")
        def fn():
            return get_current_domain()

        assert fn() == "payment.charge"

    def test_decoration_normalizes_to_lowercase(self):
        """Existing case-insensitive contract preserved through validator."""

        @domain_tag("Payment.Charge")
        def fn():
            return get_current_domain()

        assert fn() == "payment.charge"


# =============================================================================
# Chokepoint 2 — DomainContext runtime fallback
# =============================================================================


class TestDomainContextRuntimeFallbackBehavior:
    """``DomainContext(bad)`` falls back to ``OTHER_DOMAIN`` (does not raise)."""

    def test_context_falls_back_on_uuid(self):
        with DomainContext(str(uuid4())) as ctx:
            assert ctx.domain == FALLBACK_DOMAIN
            assert get_current_domain() == FALLBACK_DOMAIN

    def test_context_falls_back_on_empty(self):
        with DomainContext("") as ctx:
            assert ctx.domain == FALLBACK_DOMAIN

    def test_context_falls_back_on_too_long(self):
        with DomainContext("a" * 200) as ctx:
            assert ctx.domain == FALLBACK_DOMAIN

    def test_context_valid_domain_passes_through(self):
        with DomainContext("payment.charge") as ctx:
            assert ctx.domain == "payment.charge"
            assert get_current_domain() == "payment.charge"

    @patch("baldur.metrics.event_handlers._get_metrics")
    def test_context_emits_metric_on_rejection(self, mock_get_metrics):
        """Rejected domain triggers ``site="domain_context"`` Counter."""
        mock_metrics = MagicMock()
        mock_get_metrics.return_value = mock_metrics

        with DomainContext(str(uuid4())):
            pass

        mock_metrics.record_dlq_domain_input_rejected.assert_called_once_with(
            "domain_context"
        )

    @pytest.mark.flaky_quarantine(
        issue="545", first_seen="2026-05-28", category="mock_leak"
    )
    def test_context_emits_warning_log_on_rejection(self):
        """Rejected domain emits ``domain.input_rejected`` WARNING."""
        bad_domain = str(uuid4())
        with capture_logs() as logs:
            with DomainContext(bad_domain):
                pass

        rejected = [e for e in logs if e.get("event") == "domain.input_rejected"]
        assert len(rejected) == 1
        entry = rejected[0]
        assert entry["site"] == "domain_context"
        assert entry["reason"] == DomainRejectReason.INVALID_CHARSET.value
        assert "original_preview" in entry


# =============================================================================
# Chokepoint 3 — set_domain_context runtime fallback
# =============================================================================


class TestSetDomainContextBehavior:
    """``set_domain_context(bad)`` falls back; ``None`` passes through."""

    def test_valid_domain_sets_and_returns_token(self):
        token = set_domain_context("payment")
        assert token is not None
        assert get_current_domain() == "payment"

    def test_none_passes_through(self):
        set_domain_context("payment")
        set_domain_context(None)
        assert get_current_domain() is None

    def test_bad_domain_falls_back(self):
        set_domain_context(str(uuid4()))
        assert get_current_domain() == FALLBACK_DOMAIN

    @pytest.mark.parametrize(
        "bad_domain",
        [
            "invalid name!",
            "1payment",
            "_underscore_start",
            "payment-charge",
            "a" * 200,
        ],
    )
    def test_invalid_domains_fall_back(self, bad_domain):
        set_domain_context(bad_domain)
        assert get_current_domain() == FALLBACK_DOMAIN

    def test_returns_token_even_on_fallback(self):
        """Existing ``Token`` contract preserved on the fallback path."""
        from contextvars import Token

        token = set_domain_context(str(uuid4()))
        assert isinstance(token, Token)


class TestSetDomainContextObservabilityBehavior:
    """Runtime fallback path emits the right metric + log."""

    @patch("baldur.metrics.event_handlers._get_metrics")
    def test_emits_metric_with_site_label(self, mock_get_metrics):
        mock_metrics = MagicMock()
        mock_get_metrics.return_value = mock_metrics

        set_domain_context(str(uuid4()))

        mock_metrics.record_dlq_domain_input_rejected.assert_called_once_with(
            "set_domain_context"
        )

    @pytest.mark.flaky_quarantine(
        issue="545", first_seen="2026-05-28", category="mock_leak"
    )
    def test_emits_warning_log(self):
        with capture_logs() as logs:
            set_domain_context(str(uuid4()))

        rejected = [e for e in logs if e.get("event") == "domain.input_rejected"]
        assert len(rejected) == 1
        assert rejected[0]["site"] == "set_domain_context"
        assert rejected[0]["reason"] == DomainRejectReason.INVALID_CHARSET.value

    @patch("baldur.metrics.event_handlers._get_metrics")
    def test_no_metric_on_valid_input(self, mock_get_metrics):
        """Valid domains do NOT touch the rejection counter."""
        mock_metrics = MagicMock()
        mock_get_metrics.return_value = mock_metrics

        set_domain_context("payment.charge")

        mock_metrics.record_dlq_domain_input_rejected.assert_not_called()
