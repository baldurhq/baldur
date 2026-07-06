"""
Time Utilities with Timezone Awareness.

Provides timezone-aware datetime utilities for the baldur system.
All time operations should use these utilities to ensure consistency.

Note:
    datetime.utcnow() is deprecated in Python 3.12.
    Always use datetime.now(timezone.utc) instead.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta


def utc_now() -> datetime:
    """
    현재 UTC 시간 반환 (timezone-aware).

    Returns:
        현재 UTC 시간 (timezone-aware datetime)

    Example:
        >>> now = utc_now()
        >>> print(now.tzinfo)  # UTC
    """
    return datetime.now(UTC)


def ensure_aware(dt: datetime) -> datetime:
    """
    naive datetime을 UTC로 변환합니다.

    이미 timezone-aware인 경우 그대로 반환합니다.

    Args:
        dt: 변환할 datetime

    Returns:
        timezone-aware datetime (UTC)

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
    datetime을 ISO 8601 문자열로 변환합니다.

    Args:
        dt: 변환할 datetime (None 가능)

    Returns:
        ISO 8601 형식 문자열 또는 None

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
    ISO 8601 문자열을 datetime으로 변환합니다.

    Args:
        iso_str: ISO 8601 형식 문자열

    Returns:
        timezone-aware datetime

    Example:
        >>> dt = from_iso_string("2024-01-15T10:30:00+00:00")
        >>> print(dt.tzinfo)  # UTC
    """
    dt = datetime.fromisoformat(iso_str)
    return ensure_aware(dt)


def elapsed_seconds(start: datetime, end: datetime | None = None) -> float:
    """
    두 시간 사이의 경과 시간을 초 단위로 반환합니다.

    Args:
        start: 시작 시간
        end: 종료 시간 (None이면 현재 시간)

    Returns:
        경과 시간 (초)

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
    주어진 시간이 TTL을 초과했는지 확인합니다.

    Args:
        dt: 확인할 시간
        ttl_seconds: TTL (초)

    Returns:
        만료 여부

    Example:
        >>> created_at = utc_now() - timedelta(hours=2)
        >>> expired = is_expired(created_at, ttl_seconds=3600)  # 1시간
        >>> print(expired)  # True
    """
    return elapsed_seconds(dt) > ttl_seconds


def add_seconds(dt: datetime, seconds: float) -> datetime:
    """
    datetime에 초를 더합니다.

    Args:
        dt: 기준 시간
        seconds: 더할 초

    Returns:
        새로운 datetime
    """
    return ensure_aware(dt) + timedelta(seconds=seconds)


def format_duration(seconds: float) -> str:
    """
    초를 사람이 읽기 쉬운 형태로 포맷합니다.

    Args:
        seconds: 초 단위 시간

    Returns:
        포맷된 문자열 (예: "2h 30m 15s")

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
