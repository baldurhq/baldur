"""
Domain Input Validation.

Validates the ``domain`` string fed into ``@domain_tag`` / ``DomainContext`` /
``set_domain_context`` and ``store_failure`` / ``store_to_dlq`` so a buggy
caller cannot pollute Redis key families and the in-memory domain registries
with unbounded values (e.g. ``str(uuid4())``).

Reference:
    545
"""

from __future__ import annotations

import re
from enum import Enum

from baldur.core.exceptions import DomainValidationError

FALLBACK_DOMAIN: str = "OTHER_DOMAIN"
"""Single source of truth for the fallback label used when a domain is rejected.

Re-bound by ``baldur.metrics.registry._FALLBACK_DOMAIN`` so the metric label
domain registry shares the same fallback string as DLQ/decoder rejections.
"""

MAX_DOMAIN_LENGTH: int = 64
"""Length cap measured after ``.lower()`` normalization."""

_DOMAIN_PATTERN: re.Pattern[str] = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)*$")
"""Segmented identifier pattern.

First segment: ``[a-z][a-z0-9_]*`` — alpha start blocks UUID / digit-ID shapes.
Sub-segments (after each ``.``): ``[a-z0-9_]+`` — digit/underscore start
permitted to align with DNS RFC 1123 / Prometheus / Kubernetes / OTel
permissive non-leading-position conventions (e.g. ``region.1_primary``,
``payment.tier2``, ``auth.2fa``, ``cache.30s_ttl``).
"""


class DomainRejectReason(str, Enum):
    """Reason taxonomy for a rejected domain input.

    Bound to ``DomainValidationError.reason`` (decoration-time raise) and
    emitted as ``reason=<value>`` in the ``domain.input_rejected`` WARNING
    payload (runtime fallback path).
    """

    TOO_LONG = "too_long"
    EMPTY = "empty"
    INVALID_CHARSET = "invalid_charset"
    NOT_STRING = "not_string"


def validate_and_normalize_domain(domain: object) -> str:
    """Validate and normalize a domain string.

    Normalization order: type check → ``.lower()`` → empty/whitespace check →
    length check → regex match. The first failing check determines the
    ``DomainRejectReason`` carried by the raised ``DomainValidationError``.

    The ``NOT_STRING`` branch is reachable only from non-typed input
    boundaries (OTel baggage / Celery legacy header deserialization, HTTP
    middleware header extraction). Internal ``domain: str`` typed call sites
    are mypy/pyright-protected from this path.

    Args:
        domain: Raw input. Typed as ``object`` to make the ``NOT_STRING``
            boundary explicit.

    Returns:
        The lower-cased domain after passing all validation checks.

    Raises:
        DomainValidationError: On any rejection — caller decides whether to
            propagate (decoration time) or fall back to ``FALLBACK_DOMAIN``
            (runtime).
    """
    if not isinstance(domain, str):
        raise DomainValidationError(
            original_domain=repr(domain),
            reason=DomainRejectReason.NOT_STRING,
        )

    normalized = domain.lower()

    if not normalized or not normalized.strip():
        raise DomainValidationError(
            original_domain=domain,
            reason=DomainRejectReason.EMPTY,
        )

    if len(normalized) > MAX_DOMAIN_LENGTH:
        raise DomainValidationError(
            original_domain=domain,
            reason=DomainRejectReason.TOO_LONG,
        )

    if not _DOMAIN_PATTERN.match(normalized):
        raise DomainValidationError(
            original_domain=domain,
            reason=DomainRejectReason.INVALID_CHARSET,
        )

    return normalized


__all__ = [
    "FALLBACK_DOMAIN",
    "MAX_DOMAIN_LENGTH",
    "DomainRejectReason",
    "validate_and_normalize_domain",
]
