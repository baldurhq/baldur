"""Canonical ``Retry-After`` parser tests (#754 D11).

``Retry-After`` used to be parsed in four places with three behaviors: only the
Django middleware understood the HTTP-date form, the other three did a bare
``float()`` and silently fell back to Baldur's own backoff ladder — which is
much shorter than any wait a provider states as a date. A fleet then resumes
before the provider's stated earliest time, which is the one direction the
monotonic cooldown contract promises never to fail in.

These tests pin the parser's own contract and, separately, that the call sites
compose it rather than re-typing a coercion. The middleware's own adoption is
pinned next to its clamp in ``tests/unit/middleware/test_baldur_middleware.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from baldur.utils.retry_after import parse_retry_after

# A fixed "now" for the HTTP-date cases, so remaining-seconds arithmetic is
# exact instead of clock-dependent. The dates below are stated relative to it.
_FIXED_NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
_DATE_NOW = "Mon, 01 Jun 2026 12:00:00 GMT"
_DATE_IN_120S = "Mon, 01 Jun 2026 12:02:00 GMT"
_DATE_60S_AGO = "Mon, 01 Jun 2026 11:59:00 GMT"


def _pinned_clock():
    """Pin the parser's clock — HTTP-date parsing is relative to it."""
    return patch("baldur.utils.retry_after.utc_now", return_value=_FIXED_NOW)


class TestParseRetryAfterContract:
    """Both RFC 9110 forms in, seconds or ``None`` out, with no clamping."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("120", 120.0),
            ("0.5", 0.5),
            (120, 120.0),
            (120.5, 120.5),
        ],
        ids=["seconds-string", "sub-second-string", "int", "float"],
    )
    def test_delta_seconds_form_returns_its_value(self, raw, expected):
        """The delta-seconds form parses to itself, whatever type it arrives as.

        Already-numeric values go through the same validation on purpose: a
        caller holding a header does not have to know which form it holds.
        """
        assert parse_retry_after(raw) == pytest.approx(expected)

    def test_zero_seconds_is_a_value_not_an_absence(self):
        """Boundary: 0 is the smallest accepted wait, one step above rejection.

        Callers distinguish "wait zero seconds" from "no usable header" by the
        ``None``, so folding 0 into ``None`` here would hide a header the
        provider did send.
        """
        assert parse_retry_after("0") == 0.0

    def test_value_far_above_any_caller_ceiling_is_returned_unclamped(self):
        """A day-long wait survives the parser intact.

        Clamping is caller policy — the coordinator has ``retry_after_ceiling``
        and the middleware its own maximum — so a ceiling applied here would
        silently override both.
        """
        assert parse_retry_after("86400") == 86400.0

    def test_http_date_returns_the_seconds_remaining_until_it(self):
        """The HTTP-date form resolves against now, not to a literal."""
        with _pinned_clock():
            assert parse_retry_after(_DATE_IN_120S) == pytest.approx(120.0)

    def test_http_date_equal_to_now_returns_zero(self):
        """Boundary: a date at exactly now is expired-by-nothing, so it is a 0 wait."""
        with _pinned_clock():
            assert parse_retry_after(_DATE_NOW) == 0.0

    def test_http_date_already_past_returns_none(self):
        """Boundary: one step earlier is a negative wait, which is no wait at all."""
        with _pinned_clock():
            assert parse_retry_after(_DATE_60S_AGO) is None

    @pytest.mark.parametrize(
        "raw",
        [None, "", "soon", "-5", -5.0, []],
        ids=[
            "none",
            "empty",
            "unparseable",
            "negative-string",
            "negative",
            "wrong-type",
        ],
    )
    def test_unusable_values_return_none(self, raw):
        """Everything a caller must treat as "no header" collapses to ``None``."""
        assert parse_retry_after(raw) is None

    @pytest.mark.parametrize(
        "raw", ["nan", float("nan")], ids=["nan-string", "nan-float"]
    )
    def test_nan_is_rejected_rather_than_passed_through(self, raw):
        """The NaN rejection is load-bearing, not defensive decoration.

        ``float("nan")`` parses successfully and compares ``False`` against
        every threshold, so an unrejected NaN passes the coordinator's
        ``retry_after <= 0`` guard, becomes ``now + nan``, and installs a stored
        expiry that no later comparison can ever end.
        """
        assert parse_retry_after(raw) is None

    @pytest.mark.parametrize(
        "raw",
        ["inf", "Infinity", "+INF", "1e999", float("inf")],
        ids=["inf", "infinity-word", "signed-upper", "overflowing-literal", "float"],
    )
    def test_infinity_is_rejected_rather_than_passed_through(self, raw):
        """NaN's mirror image, and the one with the larger blast radius.

        ``float()`` accepts every spelling above, and infinity compares *greater*
        than every threshold — so an unrejected one is not dropped, it is clamped
        to whatever ceiling the caller applies. At the coordinator that is
        ``retry_after_ceiling``: a junk header installs a full hour of fleet-wide
        cooldown where "no usable header" would have installed the few-second
        ladder, and a monotonic store then holds that hour until an operator
        ``clear()``. No provider can send this — RFC 9110 delta-seconds is a
        non-negative integer.
        """
        assert parse_retry_after(raw) is None

    def test_an_integer_too_large_for_a_float_is_rejected_not_raised(self):
        """Boundary: the coercion's third failure mode, which is not a ValueError.

        ``float(10**400)`` raises ``OverflowError``. A client exposing an
        oversized ``retry_after`` attribute would otherwise propagate it out of
        the retry stage's 429 detection, where every other junk value is simply
        "no header".
        """
        assert parse_retry_after(10**400) is None


class TestRetryAfterCallSiteAdoptionBehavior:
    """The other parse sites compose the canonical parser, not their own coercion.

    Each case drives the site with an HTTP-date, which is exactly what a bare
    ``float()`` cannot read: a site that still coerced locally would report no
    header here and hand its caller the backoff ladder instead.
    """

    def test_detect_rate_limit_reads_an_http_date_on_the_exception_attribute(self):
        """The exception-attribute branch of the retry stage's 429 detection."""
        from baldur.services.retry_handler.rate_limit_detection import (
            detect_rate_limit,
        )

        class _RateLimitError(Exception):
            retry_after = _DATE_IN_120S

        with _pinned_clock():
            is_rate_limited, retry_after = detect_rate_limit(
                _RateLimitError("429 Too Many Requests")
            )

        assert is_rate_limited is True
        assert retry_after == pytest.approx(120.0)

    def test_detect_rate_limit_reads_an_http_date_on_the_response_headers(self):
        """The response-headers branch of the same function."""
        from baldur.services.retry_handler.rate_limit_detection import (
            detect_rate_limit,
        )

        class _Response:
            headers = {"Retry-After": _DATE_IN_120S}

        class _RateLimitError(Exception):
            response = _Response()

        with _pinned_clock():
            is_rate_limited, retry_after = detect_rate_limit(
                _RateLimitError("429 Too Many Requests")
            )

        assert is_rate_limited is True
        assert retry_after == pytest.approx(120.0)

    def test_default_get_retry_after_reads_an_http_date(self):
        """The decorator path's default header extractor."""
        from baldur.services.rate_limit_coordinator.helpers import (
            _default_get_retry_after,
        )

        class _Response:
            headers = {"Retry-After": _DATE_IN_120S}

        with _pinned_clock():
            assert _default_get_retry_after(_Response()) == pytest.approx(120.0)

    def test_default_get_retry_after_returns_none_without_headers(self):
        """A response object with no headers attribute is simply headerless."""
        from baldur.services.rate_limit_coordinator.helpers import (
            _default_get_retry_after,
        )

        assert _default_get_retry_after(object()) is None
