"""
Time Utilities with Timezone Awareness.

Provides timezone-aware datetime utilities for the baldur system.
All time operations should use these utilities to ensure consistency.

``utc_now()`` reads the current time through the global ``TimeProvider`` seam
(``core.time_provider``), so tests can drive it deterministically via
``set_time_provider(MockTimeProvider(...))``. The default ``SystemTimeProvider``
returns ``datetime.now(UTC)``, so production behavior is unchanged.

Note:
    datetime.utcnow() is deprecated in Python 3.12.
    Always use datetime.now(timezone.utc) (or this module) instead.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from baldur.core.time_provider import get_time_provider


def utc_now() -> datetime:
    """
    Return the current UTC time (timezone-aware).

    Reads through the global TimeProvider so tests can substitute a
    MockTimeProvider; the default SystemTimeProvider returns
    ``datetime.now(UTC)``.

    Returns:
        The current UTC time (timezone-aware datetime).

    Example:
        >>> now = utc_now()
        >>> print(now.tzinfo)  # UTC
    """
    return get_time_provider().utcnow()


def ensure_aware(dt: datetime) -> datetime:
    """
    Convert a naive datetime to UTC.

    If it is already timezone-aware, return it unchanged.

    Args:
        dt: The datetime to convert.

    Returns:
        A timezone-aware datetime (UTC).

    Example:
        >>> from datetime import datetime
        >>> naive = datetime(2024, 1, 15, 10, 30, 0)
        >>> aware = ensure_aware(naive)
        >>> print(aware.tzinfo)  # UTC
    """
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


def to_iso_string(dt: datetime | None) -> str | None:
    """
    Convert a datetime to an ISO 8601 string.

    Args:
        dt: The datetime to convert (may be None).

    Returns:
        An ISO 8601 formatted string, or None.

    Example:
        >>> now = utc_now()
        >>> iso = to_iso_string(now)
        >>> print(iso)  # "2024-01-15T10:30:00.123456+00:00"
    """
    if dt is None:
        return None
    return ensure_aware(dt).isoformat()


def from_iso_string(iso_str: str) -> datetime:
    """
    Convert an ISO 8601 string to a datetime.

    Args:
        iso_str: An ISO 8601 formatted string.

    Returns:
        A timezone-aware datetime.

    Example:
        >>> dt = from_iso_string("2024-01-15T10:30:00+00:00")
        >>> print(dt.tzinfo)  # UTC
    """
    dt = datetime.fromisoformat(iso_str)
    return ensure_aware(dt)


def elapsed_seconds(start: datetime, end: datetime | None = None) -> float:
    """
    Return the elapsed time between two datetimes in seconds.

    Args:
        start: The start time.
        end: The end time (defaults to now).

    Returns:
        The elapsed time in seconds.

    Example:
        >>> start = utc_now()
        >>> # ... some operation ...
        >>> elapsed = elapsed_seconds(start)
    """
    if end is None:
        end = utc_now()
    return (ensure_aware(end) - ensure_aware(start)).total_seconds()


def is_expired(dt: datetime, ttl_seconds: float) -> bool:
    """
    Check whether a datetime has exceeded its TTL.

    Args:
        dt: The datetime to check.
        ttl_seconds: The TTL in seconds.

    Returns:
        True if expired.

    Example:
        >>> created_at = utc_now() - timedelta(hours=2)
        >>> expired = is_expired(created_at, ttl_seconds=3600)  # 1 hour
        >>> print(expired)  # True
    """
    return elapsed_seconds(dt) > ttl_seconds


def add_seconds(dt: datetime, seconds: float) -> datetime:
    """
    Add seconds to a datetime.

    Args:
        dt: The base datetime.
        seconds: The seconds to add.

    Returns:
        A new datetime.
    """
    return ensure_aware(dt) + timedelta(seconds=seconds)


def format_duration(seconds: float) -> str:
    """
    Format seconds into a human-readable string.

    Args:
        seconds: A duration in seconds.

    Returns:
        A formatted string (e.g. "2h 30m 15s").

    Example:
        >>> print(format_duration(9015))  # "2h 30m 15s"
        >>> print(format_duration(125))   # "2m 5s"
        >>> print(format_duration(45))    # "45s"
    """
    if seconds < 60:
        return f"{seconds:.0f}s"

    minutes, secs = divmod(int(seconds), 60)
    hours, mins = divmod(minutes, 60)
    days, hrs = divmod(hours, 24)

    parts = []
    if days > 0:
        parts.append(f"{days}d")
    if hrs > 0:
        parts.append(f"{hrs}h")
    if mins > 0:
        parts.append(f"{mins}m")
    if secs > 0:
        parts.append(f"{secs}s")

    return " ".join(parts) if parts else "0s"


__all__ = [
    "utc_now",
    "ensure_aware",
    "to_iso_string",
    "from_iso_string",
    "elapsed_seconds",
    "is_expired",
    "add_seconds",
    "format_duration",
]
