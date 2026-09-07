"""
Rate limit (429) detection utility.

Extracted from RetryHandler.is_rate_limit_error() and
RetryPolicy._detect_rate_limit() to eliminate duplication
within the retry_handler package.

This is the tree's single 429 predicate: it accepts an **exception** (a client
that raises on 429) or a **returned response** (a client that hands the 429
back as a value), so every observation site — the outbound breaker stage, the
retry ladder, the tenacity bridge, the rate-limit-aware decorator — reaches the
same verdict for the same downstream answer.

Usage:
    from baldur.services.retry_handler.rate_limit_detection import detect_rate_limit

    is_limited, retry_after = detect_rate_limit(exception_or_response)
"""

from __future__ import annotations

from typing import Any

from baldur.utils.retry_after import parse_retry_after

__all__ = [
    "RATE_LIMIT_INDICATORS",
    "UNIDENTIFIED_COORDINATION_KEY",
    "detect_rate_limit",
    "failure_status_codes",
    "rate_limit_status_codes",
    "response_retry_after",
    "response_status",
]

RATE_LIMIT_INDICATORS: tuple[str, ...] = (
    "429",
    "rate limit",
    "ratelimit",
    "too many requests",
    "throttle",
    "quota exceeded",
)

# Placeholder coordination key meaning "the caller did not identify a
# downstream". It is the default of RetryPolicyConfig.domain, of the @retry
# decorator, and of both pipeline presets, so every caller who did not choose a
# name shares it. Outbound 429 coordination therefore refuses to key on it: the
# storage key carries no per-service namespace, so one shared placeholder record
# would let a 429 from one provider stall calls to an unrelated one. Lives here
# because both the retry stage and the breaker stage gate on it.
UNIDENTIFIED_COORDINATION_KEY = "default"

# Sentinel for "the object has no such attribute", kept distinct from a real
# ``None`` attribute value so a response exposing ``status_code = None`` still
# gets its ``status`` fallback consulted.
_ATTRIBUTE_MISSING = object()

# Attribute names an HTTP response may expose its status under, in priority
# order: ``status_code`` (requests, httpx, Django) then ``status`` (aiohttp
# ClientResponse, urllib3 HTTPResponse).
_STATUS_ATTRIBUTES: tuple[str, ...] = ("status_code", "status")


def _field_default_status_codes(field_name: str) -> frozenset[int]:
    """Read a middleware status-code field's default straight off the model.

    The fallback for a settings fault is the *declared* default, never an
    authored literal: a copy would silently disagree with the field the moment
    one of the two changed, and the disagreement would only ever be visible on
    the degraded path nobody exercises.
    """
    try:
        from baldur.settings.middleware import BaldurMiddlewareSettings

        return frozenset(BaldurMiddlewareSettings.model_fields[field_name].default)
    except Exception:
        # The settings module itself is unimportable — classify nothing rather
        # than guess, so a response is recorded as the success it looks like.
        return frozenset()


def failure_status_codes() -> frozenset[int]:
    """Statuses recorded as a breaker failure, inbound and outbound alike.

    Sourced from ``BALDUR_MIDDLEWARE_CB_STATUS_CODES``. The variable is named
    for the middleware that first read it; the outbound breaker stage now reads
    the same set, so one operator answer covers both directions.
    """
    try:
        from baldur.settings.middleware import get_middleware_settings

        return frozenset(get_middleware_settings().cb_status_codes)
    except Exception:
        return _field_default_status_codes("cb_status_codes")


def rate_limit_status_codes() -> frozenset[int]:
    """Statuses treated as a rate-limit answer, inbound and outbound alike.

    Sourced from ``BALDUR_MIDDLEWARE_RATE_LIMIT_CODES``. Membership here is
    independent of :func:`failure_status_codes` — a status may be in both sets,
    in which case it is both a counted failure and a cascade observation.
    """
    try:
        from baldur.settings.middleware import get_middleware_settings

        return frozenset(get_middleware_settings().rate_limit_codes)
    except Exception:
        return _field_default_status_codes("rate_limit_codes")


def response_status(value: Any) -> int | None:
    """Read an HTTP status code off a returned value, or ``None``.

    ``None`` means "this is not a response": a missing attribute, a non-``int``
    value, or an attribute access that raised. Requiring a real ``int`` is what
    keeps an ordinary return value carrying an unrelated ``status`` string out
    of the classifier.
    """
    for attribute in _STATUS_ATTRIBUTES:
        try:
            status = getattr(value, attribute, _ATTRIBUTE_MISSING)
        except Exception:
            # A property that raises is a caller-owned fault, not a status.
            return None
        if status is _ATTRIBUTE_MISSING or status is None:
            continue
        # ``bool`` is an ``int`` subclass; a flag named ``status`` is not one.
        if isinstance(status, bool) or not isinstance(status, int):
            return None
        return status
    return None


def response_retry_after(value: Any) -> float | None:
    """Read ``Retry-After`` off a response's headers, through the canonical parser.

    ``None`` for a value with no readable ``headers``, an absent header, or an
    unusable one - the caller then falls back to its own default delay.
    """
    try:
        headers = getattr(value, "headers", None)
        if headers is None:
            return None
        return parse_retry_after(headers.get("Retry-After"))
    except Exception:
        return None


def _detect_from_exception(exception: BaseException) -> tuple[bool, float | None]:
    """Classify a raised outcome by its message and type name."""
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
        # Parsed through the canonical parser, not a bare float(): a client may
        # expose the raw header string in either RFC 9110 form. Uncoerced, it
        # reaches the coordinator's numeric comparison and raises there — where
        # the fail-open wrap drops the cooldown entirely, so a real 429 installs
        # no cooldown while the consecutive counter still advances.
        retry_after = parse_retry_after(exception.retry_after)  # type: ignore[attr-defined]
    elif hasattr(exception, "response"):
        response = exception.response  # type: ignore[attr-defined]
        if hasattr(response, "headers"):
            retry_after = parse_retry_after(response.headers.get("Retry-After"))

    return is_rate_limited, retry_after


def detect_rate_limit(subject: Any) -> tuple[bool, float | None]:
    """Detect whether ``subject`` is a rate-limit (429) answer from a dependency.

    Two subject shapes, one verdict:

    - **An exception**: its message and type name are matched against the known
      rate-limit indicators, and Retry-After is read from a ``retry_after``
      attribute or an attached ``response``'s headers.
    - **Any other value**: read as a response — its ``status_code`` (or
      ``status``) must be an ``int`` listed in :func:`rate_limit_status_codes`,
      and Retry-After is read from its ``headers``. A value with no usable
      status is not rate-limited.

    Baldur's own outbound-cooldown deferral is explicitly NOT a provider rate
    limit: it means the provider was never contacted, so it is no evidence of a
    429 and must not escalate a cooldown. The heuristic would otherwise match it
    on its type name alone, whatever its message says.

    Args:
        subject: The exception raised by, or the value returned from, the call.

    Returns:
        Tuple of (is_rate_limited, retry_after_seconds).
        retry_after_seconds is None if not available.
    """
    if isinstance(subject, BaseException):
        return _detect_from_exception(subject)

    status = response_status(subject)
    if status is None or status not in rate_limit_status_codes():
        return False, None
    return True, response_retry_after(subject)
