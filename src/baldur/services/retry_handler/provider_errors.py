"""
Provider error classification for the LLM SDKs Baldur recognizes.

An LLM provider answers a refused request with structure — an HTTP status, an
error code, a body that may carry a retry hint — and its SDK keeps that
structure on the exception it raises. This module reads it and names what the
provider said, so the retry, breaker and fallback stages can act on the answer
rather than on words in a message:

- **rate_limited** — a 429 the provider will lift: wait at least its hint.
- **overloaded** — a 529 or 503: the provider is saturated, not the caller.
- **quota_exhausted** — an exhausted quota or credit balance: no wait restores
  it, so the call is not retried.
- **auth_failed** — a 401 or 403: a key problem waiting cannot fix.
- **invalid_request** — any other 4xx: the request itself was rejected, so it is
  neither retried, counted against the provider's breaker, nor sent elsewhere.
- **transient** — a 408, 409 or 5xx, or no status at all (a connection error, a
  timeout): retried on the usual ladder.

An exception the SDK raises outside its family of failed API calls (its
``APIError``) — an argument check before any request, a finish-reason check
after a 200 — carries no provider answer and gets no verdict.

Only exceptions raised by the OpenAI Python SDK, the Anthropic SDK and the
Google Gen AI SDK (``google-genai``) are classified, recognized by the module
their class (or a base class) is defined in. Every other exception gets no
verdict and keeps the behavior it always had. ``google.api_core`` exceptions
are deliberately excluded: every Google Cloud client library raises them, so
classifying them would change calls that have nothing to do with an LLM.

Nothing here imports an SDK.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from baldur.utils.retry_after import parse_retry_after

__all__ = [
    "ProviderErrorCategory",
    "ProviderVerdict",
    "classify_provider_error",
    "is_invalid_request",
    "is_non_retryable_provider_error",
]


class ProviderErrorCategory(StrEnum):
    """What a provider's refusal means for the call that received it."""

    RATE_LIMITED = "rate_limited"
    OVERLOADED = "overloaded"
    QUOTA_EXHAUSTED = "quota_exhausted"
    AUTH_FAILED = "auth_failed"
    INVALID_REQUEST = "invalid_request"
    TRANSIENT = "transient"


@dataclass(frozen=True)
class ProviderVerdict:
    """One classified provider answer.

    Attributes:
        category: What the answer means for the call.
        status: The HTTP status the SDK reported, or ``None`` when it had none
            (a connection error or a timeout).
        retry_after: The provider's stated wait in seconds, or ``None`` when it
            gave none.
        provider: ``"openai"``, ``"anthropic"`` or ``"google-genai"`` — the SDK
            that raised the exception.
    """

    category: ProviderErrorCategory
    status: int | None
    retry_after: float | None
    provider: str


# Module roots whose exceptions carry a provider's answer. A subclass defined
# elsewhere (a wrapper library subclassing the OpenAI SDK's errors) is
# recognized through its MRO.
_SDK_MODULE_ROOTS: dict[str, str] = {"openai": "openai", "anthropic": "anthropic"}
_GOOGLE_GENAI_MODULE = "google.genai"
_GOOGLE_GENAI_PROVIDER = "google-genai"

# The base class each SDK raises a failed API call from: its status errors and
# its connection / timeout errors. A status-less exception outside it was
# raised by the SDK's own checks, not by the call.
_API_ERROR_CLASS_NAME = "APIError"

# Categories under which re-sending the same request cannot help.
_NON_RETRYABLE_CATEGORIES = frozenset(
    {
        ProviderErrorCategory.QUOTA_EXHAUSTED,
        ProviderErrorCategory.AUTH_FAILED,
        ProviderErrorCategory.INVALID_REQUEST,
    }
)

# The error code (or type) the OpenAI API puts on a 429 whose cause is an
# exhausted quota rather than a burst.
_QUOTA_EXHAUSTED_CODE = "insufficient_quota"
# Anthropic answers an exhausted credit balance with a 400 whose message says so.
_CREDIT_BALANCE_PHRASE = "credit balance"
# Gemini names a daily quota in its QuotaFailure violation's quota id.
_DAILY_QUOTA_MARKER = "PerDay"
_GOOGLE_QUOTA_FAILURE_TYPE = "google.rpc.QuotaFailure"
_GOOGLE_RETRY_INFO_TYPE = "google.rpc.RetryInfo"

_STATUS_PAYMENT_REQUIRED = 402
_STATUS_BAD_REQUEST = 400
_STATUS_TOO_MANY_REQUESTS = 429
_OVERLOADED_STATUSES = frozenset({503, 529})
_AUTH_STATUSES = frozenset({401, 403})
_TRANSIENT_CLIENT_STATUSES = frozenset({408, 409})


def classify_provider_error(exc: Any) -> ProviderVerdict | None:
    """Classify an exception raised by a recognized LLM SDK.

    Returns ``None`` for anything else — an exception from any other library,
    a non-exception, or an SDK exception whose attributes could not be read.
    ``None`` means "keep today's behavior": the caller falls back to its
    general classification. Never raises.

    Args:
        exc: The exception a protected call raised.

    Returns:
        The provider's verdict, or ``None``.
    """
    try:
        if not isinstance(exc, BaseException):
            return None
        provider = _provider_of(exc)
        if provider is None:
            return None
        status = _status_of(exc)
        category = _category_of(exc, status)
        if category is None:
            return None
        return ProviderVerdict(
            category=category,
            status=status,
            retry_after=_retry_hint(exc),
            provider=provider,
        )
    except Exception:
        return None


def is_non_retryable_provider_error(exc: Any) -> bool:
    """Whether ``exc`` is a provider answer a retry cannot change.

    True for an exhausted quota, a failed authentication and a rejected
    request; False for everything else, including every exception no
    recognized SDK raised.
    """
    verdict = classify_provider_error(exc)
    return verdict is not None and verdict.category in _NON_RETRYABLE_CATEGORIES


def is_invalid_request(exc: Any) -> bool:
    """Whether ``exc`` is a provider's rejection of the request itself.

    Such an answer says nothing about the provider's health and nothing a
    different endpoint would answer differently, so it is not counted as a
    breaker failure and does not trigger a fallback.
    """
    verdict = classify_provider_error(exc)
    return (
        verdict is not None
        and verdict.category is ProviderErrorCategory.INVALID_REQUEST
    )


def _provider_of_class(cls: type) -> str | None:
    """The SDK a class is defined in, or None."""
    module = getattr(cls, "__module__", None)
    if not isinstance(module, str):
        return None
    root = module.split(".", 1)[0]
    if root in _SDK_MODULE_ROOTS:
        return _SDK_MODULE_ROOTS[root]
    if module == _GOOGLE_GENAI_MODULE or module.startswith(_GOOGLE_GENAI_MODULE + "."):
        return _GOOGLE_GENAI_PROVIDER
    return None


def _provider_of(exc: BaseException) -> str | None:
    """The SDK that defined ``exc``'s class or one of its bases, or None."""
    for cls in type(exc).__mro__:
        provider = _provider_of_class(cls)
        if provider is not None:
            return provider
    return None


def _is_api_error(exc: BaseException) -> bool:
    """Whether ``exc`` belongs to its SDK's family of failed API calls."""
    return any(
        cls.__name__ == _API_ERROR_CLASS_NAME and _provider_of_class(cls) is not None
        for cls in type(exc).__mro__
    )


def _int_attribute(exc: BaseException, name: str) -> int | None:
    value = getattr(exc, name, None)
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return int(value)


def _status_of(exc: BaseException) -> int | None:
    """``status_code`` (OpenAI, Anthropic) else an integer ``code`` (Gen AI)."""
    status = _int_attribute(exc, "status_code")
    if status is not None:
        return status
    return _int_attribute(exc, "code")


def _category_of(
    exc: BaseException, status: int | None
) -> ProviderErrorCategory | None:
    """Apply the category table; the first matching row wins.

    ``None`` for a status-less exception the SDK raised outside a call — no
    provider answered it.
    """
    if _is_quota_exhausted(exc, status):
        return ProviderErrorCategory.QUOTA_EXHAUSTED
    if status is None:
        return ProviderErrorCategory.TRANSIENT if _is_api_error(exc) else None
    if status == _STATUS_TOO_MANY_REQUESTS:
        return ProviderErrorCategory.RATE_LIMITED
    if status in _OVERLOADED_STATUSES:
        return ProviderErrorCategory.OVERLOADED
    if status in _AUTH_STATUSES:
        return ProviderErrorCategory.AUTH_FAILED
    if status in _TRANSIENT_CLIENT_STATUSES or 500 <= status <= 599:
        return ProviderErrorCategory.TRANSIENT
    if 400 <= status <= 499:
        return ProviderErrorCategory.INVALID_REQUEST
    # A status outside the error ranges (a response the SDK could not parse)
    # is no rejection of the request: it is handled like an answer that never
    # arrived.
    return ProviderErrorCategory.TRANSIENT


def _is_quota_exhausted(exc: BaseException, status: int | None) -> bool:
    if status == _STATUS_PAYMENT_REQUIRED:
        return True
    if status == _STATUS_TOO_MANY_REQUESTS:
        for name in ("code", "type"):
            if getattr(exc, name, None) == _QUOTA_EXHAUSTED_CODE:
                return True
        return _has_daily_quota_violation(exc)
    if status == _STATUS_BAD_REQUEST:
        return _CREDIT_BALANCE_PHRASE in str(exc).lower()
    return False


def _google_error_details(exc: BaseException) -> list[Any]:
    """The ``details`` list of a Gen AI error body, or an empty list."""
    body = getattr(exc, "details", None)
    if not isinstance(body, dict):
        return []
    error = body.get("error")
    if isinstance(error, dict) and isinstance(error.get("details"), list):
        return list(error["details"])
    details = body.get("details")
    if isinstance(details, list):
        return list(details)
    return []


def _detail_is(detail: Any, type_suffix: str) -> bool:
    return isinstance(detail, dict) and str(detail.get("@type", "")).endswith(
        type_suffix
    )


def _has_daily_quota_violation(exc: BaseException) -> bool:
    for detail in _google_error_details(exc):
        if not _detail_is(detail, _GOOGLE_QUOTA_FAILURE_TYPE):
            continue
        violations = detail.get("violations")
        if not isinstance(violations, list):
            continue
        for violation in violations:
            if isinstance(violation, dict) and _DAILY_QUOTA_MARKER in str(
                violation.get("quotaId", "")
            ):
                return True
    return False


def _retry_hint(exc: BaseException) -> float | None:
    """The provider's stated wait: ``retry-after-ms``, ``retry-after``, then Gemini's body."""
    hint = _header_retry_hint(getattr(exc, "response", None))
    if hint is not None:
        return hint
    return _google_retry_delay(exc)


def _header_retry_hint(response: Any) -> float | None:
    try:
        headers = getattr(response, "headers", None)
        if headers is None:
            return None
        raw_ms = headers.get("retry-after-ms")
        if raw_ms not in (None, ""):
            try:
                seconds: float | None = float(raw_ms) / 1000.0
            except (TypeError, ValueError, OverflowError):
                seconds = None
            hint = parse_retry_after(seconds)
            if hint is not None:
                return hint
        return parse_retry_after(headers.get("retry-after"))
    except Exception:
        return None


def _google_retry_delay(exc: BaseException) -> float | None:
    """Gemini's ``RetryInfo.retryDelay`` (``"45s"`` / ``"45.47s"``) in seconds."""
    for detail in _google_error_details(exc):
        if not _detail_is(detail, _GOOGLE_RETRY_INFO_TYPE):
            continue
        raw = detail.get("retryDelay")
        if not isinstance(raw, str):
            continue
        text = raw.strip()
        if text.endswith("s"):
            text = text[:-1]
        try:
            seconds: float | None = float(text)
        except (ValueError, OverflowError):
            seconds = None
        hint = parse_retry_after(seconds)
        if hint is not None:
            return hint
    return None
