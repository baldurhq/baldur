"""
Timezone Utilities for Baldur System

Framework-agnostic timezone utilities that replace django.utils.timezone.
Uses Python's standard library datetime and zoneinfo modules.

Now integrated with TimeProvider for testable time operations.

Usage:
    from baldur.core.timezone import now, make_aware, is_aware
"""

from __future__ import annotations

from datetime import UTC, datetime
from datetime import timezone as tz

try:
    from zoneinfo import ZoneInfo
except ImportError:
    # Python < 3.9 fallback
    from backports.zoneinfo import ZoneInfo  # type: ignore


# Default timezone (can be configured)
_default_timezone: tz = UTC


def set_default_timezone(timezone_name: str) -> None:
    """
    Set the default timezone for the baldur system.

    Args:
        timezone_name: Timezone name (e.g., "UTC", "Asia/Seoul")
    """
    global _default_timezone
    _default_timezone = UTC if timezone_name == "UTC" else ZoneInfo(timezone_name)  # type: ignore


def get_default_timezone() -> tz:
    """Get the current default timezone."""
    return _default_timezone


def now() -> datetime:
    """
    Return the current datetime with timezone info.

    Uses the global TimeProvider if available for testability.
    Equivalent to django.utils.timezone.now()
    """
    try:
        from baldur.core.time_provider import get_time_provider

        return get_time_provider().now()
    except ImportError:
        # Fallback if time_provider not available
        return datetime.now(_default_timezone)


def utcnow() -> datetime:
    """
    Return the current UTC datetime with timezone info.

    Uses the global TimeProvider if available for testability.
    """
    try:
        from baldur.core.time_provider import get_time_provider

        return get_time_provider().utcnow()
    except ImportError:
        return datetime.now(UTC)


def is_aware(value: datetime) -> bool:
    """
    Check if a datetime is timezone-aware.

    Equivalent to django.utils.timezone.is_aware()
    """
    return value.tzinfo is not None and value.utcoffset() is not None


def is_naive(value: datetime) -> bool:
    """
    Check if a datetime is timezone-naive.

    Equivalent to django.utils.timezone.is_naive()
    """
    return not is_aware(value)


def make_aware(
    value: datetime,
    timezone: tz | None = None,
) -> datetime:
    """
    Make a naive datetime object timezone-aware.

    Equivalent to django.utils.timezone.make_aware()

    Args:
        value: A naive datetime object
        timezone: The timezone to use (defaults to default timezone)

    Returns:
        A timezone-aware datetime object
    """
    if is_aware(value):
        return value

    tz_to_use = timezone or _default_timezone
    return value.replace(tzinfo=tz_to_use)


def make_naive(
    value: datetime,
    timezone: tz | None = None,
) -> datetime:
    """
    Make a timezone-aware datetime object naive.

    Equivalent to django.utils.timezone.make_naive()

    Args:
        value: A timezone-aware datetime object
        timezone: The timezone to convert to before making naive

    Returns:
        A naive datetime object
    """
    if is_naive(value):
        return value

    tz_to_use = timezone or _default_timezone
    # Convert to target timezone first
    if isinstance(tz_to_use, tz):
        value = value.astimezone(tz_to_use)
    else:
        value = value.astimezone(tz_to_use)

    return value.replace(tzinfo=None)


def localtime(value: datetime | None = None, timezone: tz | None = None) -> datetime:
    """
    Convert an aware datetime to local time.

    Equivalent to django.utils.timezone.localtime()

    Args:
        value: A datetime object (defaults to now())
        timezone: The timezone to convert to (defaults to default timezone)

    Returns:
        A datetime object in the specified timezone
    """
    if value is None:
        value = now()

    if is_naive(value):
        value = make_aware(value)

    tz_to_use = timezone or _default_timezone
    return value.astimezone(tz_to_use)


def timedelta_seconds(seconds: int) -> datetime:
    """
    Return current time plus specified seconds.

    Args:
        seconds: Number of seconds to add

    Returns:
        Current time plus seconds
    """
    from datetime import timedelta

    return now() + timedelta(seconds=seconds)


def timedelta_hours(hours: int) -> datetime:
    """
    Return current time plus specified hours.

    Args:
        hours: Number of hours to add

    Returns:
        Current time plus hours
    """
    from datetime import timedelta

    return now() + timedelta(hours=hours)


def timedelta_days(days: int) -> datetime:
    """
    Return current time plus specified days.

    Args:
        days: Number of days to add

    Returns:
        Current time plus days
    """
    from datetime import timedelta

    return now() + timedelta(days=days)
