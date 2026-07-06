"""
Rate limit detection utility unit tests.

Test target: services/retry_handler/rate_limit_detection.py
- RATE_LIMIT_INDICATORS contract values
- detect_rate_limit() behavior (detection, Retry-After extraction, edge cases)

Reference:
    docs/baldur/middleware_system/310_FUNCTIONAL_DUPLICATION_ELIMINATION.md §3.1.6.1
"""

from __future__ import annotations

from unittest.mock import MagicMock

from baldur.services.retry_handler.rate_limit_detection import (
    RATE_LIMIT_INDICATORS,
    detect_rate_limit,
)

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
