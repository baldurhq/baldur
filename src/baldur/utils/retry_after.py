"""
Canonical ``Retry-After`` header parsing.

One parser for every site that reads a provider's ``Retry-After``: the Django
middleware, the retry stage's 429 detection, the coordinator's decorator helper
and the coordinator boundary itself. Parsing it in four places produced four
behaviors — three of them silently dropped the HTTP-date form and fell back to
Baldur's own (much shorter) backoff ladder, which resumes a fleet long before a
provider's stated earliest time.

Scope: this returns **seconds only, unclamped**. An upper bound on an honored
header is caller policy (the coordinator has ``retry_after_ceiling``, the
middleware its own maximum), so each call site applies its own after parsing.
"""

from __future__ import annotations

import math
from email.utils import parsedate_to_datetime
from typing import Any

from baldur.utils.time import utc_now

__all__ = [
    "parse_retry_after",
]


def parse_retry_after(raw: Any) -> float | None:
    """Parse a ``Retry-After`` header value into a wait in seconds.

    Both RFC 9110 forms are accepted: delta-seconds (``"120"``) and HTTP-date
    (``"Fri, 31 Dec 2025 23:59:59 GMT"``), the latter returned as the remaining
    seconds from now.

    Args:
        raw: The raw header value. Already-numeric values pass through the same
            validation, so a caller need not know which form it holds.

    Returns:
        A finite, non-negative wait in seconds, or ``None`` when the value is
        absent, unparseable, negative, NaN, infinite, or an HTTP-date already in
        the past. Every caller treats ``None`` as "no usable header" and falls
        back to its own policy.

    Note:
        The non-finite rejection is load-bearing, not decoration. Neither NaN nor
        infinity is a ``Retry-After`` any provider can send — RFC 9110's
        delta-seconds is a non-negative integer — yet ``float()`` accepts the
        spellings of both (``"nan"``, ``"inf"``, ``"Infinity"``, ``"1e999"``).
        An unrejected NaN compares ``False`` against every threshold and becomes
        a stored expiry no later comparison can end; an unrejected infinity
        compares greater than every threshold and pins the caller at its own
        ceiling — an hour of fleet-wide cooldown from a junk header, which a
        monotonic store will then hold for the full hour.
    """
    if raw is None or raw == "":
        return None

    seconds: float | None
    try:
        seconds = float(raw)
    except (ValueError, TypeError, OverflowError):
        # OverflowError is the int-too-large-to-convert case; like the other
        # two it means "not a delta-seconds", so the date form gets its turn.
        seconds = _parse_http_date_seconds(raw)

    if seconds is None:
        return None
    if not math.isfinite(seconds) or seconds < 0:
        return None
    return seconds


def _parse_http_date_seconds(raw: Any) -> float | None:
    """Return the seconds remaining until an HTTP-date, or None if unusable."""
    try:
        remaining: float = (parsedate_to_datetime(raw) - utc_now()).total_seconds()
    except Exception:
        return None
    return remaining
