"""
Rate limit (429) detection utility.

Extracted from RetryHandler.is_rate_limit_error() and
RetryPolicy._detect_rate_limit() to eliminate duplication
within the retry_handler package.

Usage:
    from baldur.services.retry_handler.rate_limit_detection import detect_rate_limit

    is_limited, retry_after = detect_rate_limit(exception)
"""

from __future__ import annotations

__all__ = [
    "RATE_LIMIT_INDICATORS",
    "detect_rate_limit",
]

RATE_LIMIT_INDICATORS: tuple[str, ...] = (
    "429",
    "rate limit",
    "ratelimit",
    "too many requests",
    "throttle",
    "quota exceeded",
)


def detect_rate_limit(exception: Exception) -> tuple[bool, float | None]:
    """Detect if an exception indicates a rate limit (429) error.

    Checks exception message and type name against known rate limit
    indicators, and extracts Retry-After value if available.

    Args:
        exception: The exception to check.

    Baldur's own outbound-cooldown deferral is explicitly NOT a provider rate
    limit: it means the provider was never contacted, so it is no evidence of a
    429 and must not escalate a cooldown. The heuristic below would otherwise
    match it on its type name alone, whatever its message says.

    Returns:
        Tuple of (is_rate_limited, retry_after_seconds).
        retry_after_seconds is None if not available.
    """
    # Local import: keeps the coordinator package out of this module's import
    # graph, matching how the retry policy defers the same symbol.
    from baldur.services.rate_limit_coordinator.models import RateLimitDeferredError

    if isinstance(exception, RateLimitDeferredError):
        return False, None

    error_str = str(exception).lower()
    error_type = type(exception).__name__.lower()

    is_rate_limited = any(
        indicator in error_str or indicator in error_type
        for indicator in RATE_LIMIT_INDICATORS
    )

    retry_after: float | None = None
    if hasattr(exception, "retry_after"):
        retry_after = exception.retry_after  # type: ignore[attr-defined]
    elif hasattr(exception, "response"):
        response = exception.response  # type: ignore[attr-defined]
        if hasattr(response, "headers"):
            retry_after_header = response.headers.get("Retry-After")
            if retry_after_header:
                try:
                    retry_after = float(retry_after_header)
                except (ValueError, TypeError):
                    pass

    return is_rate_limited, retry_after
