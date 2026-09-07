"""
Rate limit detection utility unit tests.

Test target: services/retry_handler/rate_limit_detection.py
- RATE_LIMIT_INDICATORS contract values
- detect_rate_limit() behavior (detection, Retry-After extraction, edge cases)
"""

from __future__ import annotations

from datetime import timedelta
from email.utils import format_datetime
from unittest.mock import MagicMock, patch

import pytest

from baldur.services.retry_handler.rate_limit_detection import (
    RATE_LIMIT_INDICATORS,
    UNIDENTIFIED_COORDINATION_KEY,
    detect_rate_limit,
    failure_status_codes,
    rate_limit_status_codes,
    response_retry_after,
    response_status,
)
from baldur.settings.middleware import reset_middleware_settings
from baldur.utils.retry_after import parse_retry_after
from baldur.utils.time import utc_now

# =============================================================================
# Contract Tests
# =============================================================================


class TestRateLimitDetectionContract:
    """RATE_LIMIT_INDICATORS constants and detect_rate_limit return type contract."""

    def test_rate_limit_indicators_is_tuple(self):
        """RATE_LIMIT_INDICATORS is a tuple of strings."""
        assert isinstance(RATE_LIMIT_INDICATORS, tuple)
        assert all(isinstance(i, str) for i in RATE_LIMIT_INDICATORS)

    def test_rate_limit_indicators_contains_429(self):
        """429 status code string is in indicators."""
        assert "429" in RATE_LIMIT_INDICATORS

    def test_rate_limit_indicators_contains_rate_limit(self):
        """'rate limit' keyword is in indicators."""
        assert "rate limit" in RATE_LIMIT_INDICATORS

    def test_rate_limit_indicators_contains_ratelimit(self):
        """'ratelimit' (no space) keyword is in indicators."""
        assert "ratelimit" in RATE_LIMIT_INDICATORS

    def test_rate_limit_indicators_contains_too_many_requests(self):
        """'too many requests' keyword is in indicators."""
        assert "too many requests" in RATE_LIMIT_INDICATORS

    def test_rate_limit_indicators_contains_throttle(self):
        """'throttle' keyword is in indicators."""
        assert "throttle" in RATE_LIMIT_INDICATORS

    def test_rate_limit_indicators_contains_quota_exceeded(self):
        """'quota exceeded' keyword is in indicators."""
        assert "quota exceeded" in RATE_LIMIT_INDICATORS

    def test_detect_rate_limit_returns_tuple_of_two(self):
        """detect_rate_limit returns (bool, float | None) tuple."""
        result = detect_rate_limit(Exception("test"))
        assert isinstance(result, tuple)
        assert len(result) == 2
        assert isinstance(result[0], bool)


# =============================================================================
# Behavior Tests — Detection
# =============================================================================


class TestDetectRateLimitDetectionBehavior:
    """detect_rate_limit() 429 detection behavior."""

    def test_detects_429_in_message(self):
        """Exception message containing '429' is detected as rate limited."""
        is_limited, _ = detect_rate_limit(Exception("HTTP 429 Too Many Requests"))
        assert is_limited is True

    def test_detects_rate_limit_in_message(self):
        """Exception message containing 'rate limit' is detected."""
        is_limited, _ = detect_rate_limit(Exception("Rate Limit exceeded"))
        assert is_limited is True

    def test_detects_ratelimit_no_space(self):
        """Exception message containing 'ratelimit' (no space) is detected."""
        is_limited, _ = detect_rate_limit(Exception("RateLimit error"))
        assert is_limited is True

    def test_detects_throttle_in_message(self):
        """Exception message containing 'throttle' is detected."""
        is_limited, _ = detect_rate_limit(Exception("Request throttled"))
        assert is_limited is True

    def test_detects_quota_exceeded(self):
        """Exception message containing 'quota exceeded' is detected."""
        is_limited, _ = detect_rate_limit(Exception("API quota exceeded"))
        assert is_limited is True

    def test_detects_too_many_requests(self):
        """Exception message containing 'too many requests' is detected."""
        is_limited, _ = detect_rate_limit(Exception("Too Many Requests"))
        assert is_limited is True

    def test_detects_indicator_in_exception_type_name(self):
        """Rate limit indicator in exception class name is detected."""

        class RateLimitError(Exception):
            pass

        is_limited, _ = detect_rate_limit(RateLimitError("some error"))
        assert is_limited is True

    def test_normal_error_not_detected(self):
        """Normal errors without rate limit indicators are not detected."""
        is_limited, _ = detect_rate_limit(ConnectionError("connection refused"))
        assert is_limited is False

    def test_unrelated_error_message_not_detected(self):
        """Error message without any indicator keyword is not detected."""
        is_limited, _ = detect_rate_limit(ValueError("invalid input value"))
        assert is_limited is False

    def test_case_insensitive_detection(self):
        """Detection is case-insensitive (message lowered before comparison)."""
        is_limited, _ = detect_rate_limit(Exception("RATE LIMIT EXCEEDED"))
        assert is_limited is True


# =============================================================================
# Behavior Tests — Retry-After Extraction
# =============================================================================


class TestDetectRateLimitRetryAfterBehavior:
    """detect_rate_limit() Retry-After extraction behavior."""

    def test_extracts_retry_after_from_attribute(self):
        """Extracts retry_after from exception.retry_after attribute."""
        exc = Exception("throttled")
        exc.retry_after = 30.0  # type: ignore[attr-defined]
        _, retry_after = detect_rate_limit(exc)
        assert retry_after == 30.0

    def test_extracts_retry_after_from_response_headers(self):
        """Extracts Retry-After from exception.response.headers."""
        exc = Exception("429")
        mock_response = MagicMock()
        mock_response.headers = {"Retry-After": "60"}
        exc.response = mock_response  # type: ignore[attr-defined]
        _, retry_after = detect_rate_limit(exc)
        assert retry_after == 60.0

    def test_retry_after_attribute_takes_precedence_over_header(self):
        """retry_after attribute is checked before response headers."""
        exc = Exception("429")
        exc.retry_after = 10.0  # type: ignore[attr-defined]
        mock_response = MagicMock()
        mock_response.headers = {"Retry-After": "999"}
        exc.response = mock_response  # type: ignore[attr-defined]
        _, retry_after = detect_rate_limit(exc)
        assert retry_after == 10.0

    def test_no_retry_after_returns_none(self):
        """Returns None when no Retry-After info available."""
        _, retry_after = detect_rate_limit(Exception("429 error"))
        assert retry_after is None

    def test_invalid_retry_after_header_returns_none(self):
        """Invalid (non-numeric) Retry-After header results in None."""
        exc = Exception("429")
        mock_response = MagicMock()
        mock_response.headers = {"Retry-After": "invalid"}
        exc.response = mock_response  # type: ignore[attr-defined]
        _, retry_after = detect_rate_limit(exc)
        assert retry_after is None

    def test_missing_headers_attribute_returns_none(self):
        """Response without headers attribute results in None."""
        exc = Exception("429")
        exc.response = object()  # type: ignore[attr-defined]
        _, retry_after = detect_rate_limit(exc)
        assert retry_after is None

    def test_empty_retry_after_header_returns_none(self):
        """Empty Retry-After header string results in None."""
        exc = Exception("429")
        mock_response = MagicMock()
        mock_response.headers = {"Retry-After": ""}
        exc.response = mock_response  # type: ignore[attr-defined]
        _, retry_after = detect_rate_limit(exc)
        assert retry_after is None


# =============================================================================
# Response doubles — the two calling conventions a client may use
# =============================================================================


def _response(**attributes):
    """Build a returned-value double exposing exactly the given attributes.

    Attribute *presence* is what the classifier branches on, so a double that
    always carries both ``status_code`` and ``status`` could never reach the
    fallback branch.
    """
    return type("FakeResponse", (), attributes)()


class _RaisingStatus:
    """A response whose status is a property that raises."""

    @property
    def status_code(self):
        raise RuntimeError("status unavailable")


# =============================================================================
# Behavior Tests — Returned-response branch
# =============================================================================


class TestDetectRateLimitResponseBehavior:
    """detect_rate_limit() classifies a value a client returned instead of raising."""

    @pytest.mark.parametrize(
        ("attributes", "expected"),
        [
            ({"status_code": 429}, True),
            ({"status": 429}, True),
            ({"status_code": "429"}, False),
            ({"status_code": None, "status": 429}, True),
            ({}, False),
            ({"status_code": True}, False),
        ],
        ids=[
            "status_code_int",
            "status_attr_int",
            "status_code_str",
            "status_code_none_falls_back",
            "no_status_attribute",
            "bool_is_not_a_status",
        ],
    )
    def test_returned_value_is_rate_limited_only_for_an_int_429(
        self, attributes, expected
    ):
        """Only a real integer status listed in the rate-limit set is a 429.

        A client that hands its answer back instead of raising is the shape the
        exception-only classifier could not see at all; the guards around it are
        what keep an ordinary return value carrying an unrelated ``status``
        field out of the breaker's cascade.
        """
        is_limited, _ = detect_rate_limit(_response(**attributes))
        assert is_limited is expected

    def test_a_status_property_that_raises_is_not_rate_limited(self):
        """Fail-open: a caller-owned fault degrades to 'not a 429', never propagates."""
        is_limited, retry_after = detect_rate_limit(_RaisingStatus())
        assert is_limited is False
        assert retry_after is None

    @pytest.mark.parametrize(
        ("status", "expected"),
        [(428, False), (429, True), (430, False), (503, False)],
        ids=["just_below", "at_429", "just_above", "failure_status_only"],
    )
    def test_only_the_configured_rate_limit_status_classifies(self, status, expected):
        """Membership, not a numeric range: 428/430 and a 5xx are not 429s.

        ``failure_status_codes()`` and ``rate_limit_status_codes()`` are
        independent sets, so a 503 records a breaker failure without ever
        reaching the cascade.
        """
        is_limited, _ = detect_rate_limit(_response(status_code=status))
        assert is_limited is expected

    def test_a_429_response_carries_its_retry_after_through(self):
        """Retry-After travels with the verdict so the cooldown honours it."""
        response = _response(status_code=429, headers={"Retry-After": "45"})
        assert detect_rate_limit(response) == (True, 45.0)

    def test_a_429_response_without_headers_reports_no_retry_after(self):
        """No header is not a fault — the caller falls back to its own delay."""
        assert detect_rate_limit(_response(status_code=429)) == (True, None)

    def test_a_non_429_response_reads_no_retry_after_at_all(self):
        """A 200 carrying a Retry-After header is still not a rate-limit answer."""
        response = _response(status_code=200, headers={"Retry-After": "45"})
        assert detect_rate_limit(response) == (False, None)

    def test_a_deferral_exception_is_never_a_provider_429(self):
        """Baldur's own cooldown deferral means the provider was never called.

        Its type name carries a rate-limit indicator, so the exception
        heuristic would otherwise escalate a cooldown from Baldur's own
        decision not to call — a self-sustaining loop.
        """
        from baldur.services.rate_limit_coordinator.models import RateLimitDeferredError

        deferral = RateLimitDeferredError(key="payment", not_before=1.0)
        assert detect_rate_limit(deferral) == (False, None)


# =============================================================================
# Contract Tests — response_status()
# =============================================================================


class TestResponseStatusContract:
    """response_status() reads a status off a value, or reports 'not a response'."""

    def test_status_code_attribute_is_read_first(self):
        """``status_code`` (requests/httpx/Django) outranks ``status``."""
        assert response_status(_response(status_code=502, status=200)) == 502

    def test_status_attribute_is_the_fallback(self):
        """``status`` (aiohttp/urllib3) is consulted when ``status_code`` is absent."""
        assert response_status(_response(status=204)) == 204

    def test_a_none_status_code_falls_through_to_status(self):
        """A declared-but-empty ``status_code`` must not shadow a real ``status``."""
        assert response_status(_response(status_code=None, status=429)) == 429

    @pytest.mark.parametrize("value", [True, False], ids=["true", "false"])
    def test_a_bool_is_not_a_status(self, value):
        """``bool`` subclasses ``int``; a flag named ``status`` is not a status."""
        assert response_status(_response(status=value)) is None

    @pytest.mark.parametrize(
        "value", ["200", 200.0, object()], ids=["str", "float", "object"]
    )
    def test_a_non_int_status_is_not_a_status(self, value):
        """Requiring a real ``int`` is what keeps ordinary return values out."""
        assert response_status(_response(status_code=value)) is None

    def test_a_value_with_no_status_attributes_is_not_a_response(self):
        """An ordinary business object reports None, not a guess."""
        assert response_status(object()) is None

    def test_a_property_that_raises_is_not_a_status(self):
        """A caller-owned fault degrades to 'not a response', never propagates."""
        assert response_status(_RaisingStatus()) is None


# =============================================================================
# Behavior Tests — response_retry_after()
# =============================================================================


class TestResponseRetryAfterBehavior:
    """response_retry_after() reads Retry-After through the canonical parser."""

    def test_delta_seconds_form_is_parsed(self):
        """RFC 9110 delta-seconds: the header's own number, in seconds."""
        assert response_retry_after(_response(headers={"Retry-After": "90"})) == 90.0

    def test_http_date_form_is_parsed_into_remaining_seconds(self):
        """RFC 9110 HTTP-date is read by the shared parser, not a local float().

        A local parse would drop this form and fall the fleet back to Baldur's
        own much shorter ladder, resuming long before the provider's stated
        earliest time.
        """
        header = format_datetime(utc_now() + timedelta(seconds=120))

        parsed = response_retry_after(_response(headers={"Retry-After": header}))

        assert parsed is not None
        assert parsed == pytest.approx(parse_retry_after(header), abs=2.0)

    def test_a_value_with_no_headers_reports_none(self):
        """A headerless value is not a fault — the caller uses its own default."""
        assert response_retry_after(object()) is None

    def test_a_none_headers_attribute_reports_none(self):
        """``headers = None`` is the shape a stubbed client commonly exposes."""
        assert response_retry_after(_response(headers=None)) is None

    def test_an_absent_retry_after_key_reports_none(self):
        """Headers present, this one absent."""
        assert response_retry_after(_response(headers={"X-Other": "1"})) is None

    def test_a_headers_property_that_raises_reports_none(self):
        """Reading caller-supplied attributes is part of this site's fault surface."""

        class RaisingHeaders:
            @property
            def headers(self):
                raise RuntimeError("headers unavailable")

        assert response_retry_after(RaisingHeaders()) is None


# =============================================================================
# Contract Tests — the shared status vocabulary
# =============================================================================


class TestStatusVocabularyContract:
    """One operator answer decides both directions' status classification."""

    def setup_method(self):
        reset_middleware_settings()

    def teardown_method(self):
        reset_middleware_settings()

    def test_failure_status_codes_default_set(self):
        """Design default: the four gateway-class 5xx statuses."""
        assert failure_status_codes() == frozenset({500, 502, 503, 504})

    def test_rate_limit_status_codes_default_set(self):
        """Design default: 429 alone."""
        assert rate_limit_status_codes() == frozenset({429})

    def test_an_operator_override_reaches_the_failure_set(self, monkeypatch):
        """The outbound stage reads the same variable the middleware does.

        An operator who narrows the list must change breaker behaviour on every
        framework, not only on the inbound path that first read it.
        """
        monkeypatch.setenv("BALDUR_MIDDLEWARE_CB_STATUS_CODES", "[503]")
        reset_middleware_settings()

        assert failure_status_codes() == frozenset({503})

    def test_an_operator_override_reaches_the_rate_limit_set(self, monkeypatch):
        """Same variable, same reader, for the cascade's vocabulary."""
        monkeypatch.setenv("BALDUR_MIDDLEWARE_RATE_LIMIT_CODES", "[429,529]")
        reset_middleware_settings()

        assert rate_limit_status_codes() == frozenset({429, 529})

    def test_a_status_in_both_sets_is_a_member_of_both(self, monkeypatch):
        """Membership is non-exclusive: one status can be failure AND cascade.

        The overlap warning this replaced described an if/elif dispatch in which
        an overlapping code silently bypassed cascade detection.
        """
        monkeypatch.setenv("BALDUR_MIDDLEWARE_CB_STATUS_CODES", "[429,503]")
        reset_middleware_settings()

        assert 429 in failure_status_codes()
        assert 429 in rate_limit_status_codes()

    def test_a_settings_fault_falls_back_to_the_declared_defaults(self):
        """A degraded settings read classifies by the design default, not nothing.

        Returning an empty set here would record a 5xx storm as a run of
        successes, on the one path nobody exercises.
        """
        with patch(
            "baldur.settings.middleware.get_middleware_settings",
            side_effect=RuntimeError("settings unavailable"),
        ):
            assert failure_status_codes() == frozenset({500, 502, 503, 504})
            assert rate_limit_status_codes() == frozenset({429})

    def test_unidentified_coordination_key_equals_the_retry_config_default(self):
        """The two gates that refuse to key on it must name the same string.

        The breaker stage and the retry stage both refuse fleet-wide
        coordination for the placeholder domain. If the constant and the config
        default drifted apart, one of the two gates would stop firing and a 429
        from one provider could stall calls to an unrelated one.
        """
        from baldur.services.retry_handler.models import RetryPolicyConfig

        assert RetryPolicyConfig().domain == UNIDENTIFIED_COORDINATION_KEY
