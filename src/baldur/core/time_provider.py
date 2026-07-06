"""
Time Provider for Clock Skew Resilience

Provides a testable abstraction over system time with support for
clock skew tolerance in distributed systems.

This module enables:
- Testable time operations via MockTimeProvider
- Clock skew tolerance for distributed idempotency checks
- Framework-agnostic time handling
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import UTC, datetime, timedelta, tzinfo

try:
    from zoneinfo import ZoneInfo
except ImportError:
    # Python < 3.9 fallback
    from backports.zoneinfo import ZoneInfo  # type: ignore


class TimeProvider(ABC):
    """
    Abstract base class for time providers.

    Enables dependency injection of time for testing and allows
    clock skew tolerance configuration.
    """

    @abstractmethod
    def now(self) -> datetime:
        """
        Return the current datetime with timezone info.

        Returns:
            Timezone-aware datetime representing current time.
        """
        pass

    @abstractmethod
    def utcnow(self) -> datetime:
        """
        Return the current UTC datetime with timezone info.

        Returns:
            UTC timezone-aware datetime.
        """
        pass

    def is_within_tolerance(
        self,
        timestamp: datetime,
        tolerance: timedelta,
        reference_time: datetime | None = None,
    ) -> bool:
        """
        Check if a timestamp is within tolerance of reference time.

        Useful for distributed systems where clocks may be slightly
        out of sync (clock skew).

        Args:
            timestamp: The timestamp to check
            tolerance: Maximum allowed difference from reference time
            reference_time: Reference time (defaults to now())

        Returns:
            True if timestamp is within tolerance
        """
        ref = reference_time or self.now()

        # Make both timezone-aware if needed
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=UTC)
        if ref.tzinfo is None:
            ref = ref.replace(tzinfo=UTC)

        diff = abs(ref - timestamp)
        return diff <= tolerance

    def get_skew_adjusted_window(
        self,
        start: datetime,
        end: datetime,
        skew_tolerance: timedelta,
    ) -> tuple[datetime, datetime]:
        """
        Expand a time window to account for clock skew.

        Args:
            start: Window start time
            end: Window end time
            skew_tolerance: Clock skew tolerance

        Returns:
            Tuple of (adjusted_start, adjusted_end) with tolerance added
        """
        return (start - skew_tolerance, end + skew_tolerance)

    def now_with_skew_tolerance(
        self,
        tolerance_seconds: float = 30.0,
    ) -> tuple[datetime, datetime]:
        """
        Get time window for clock skew tolerance.

        Returns a tuple of (lower_bound, upper_bound) representing the
        acceptable time range considering clock skew.

        Args:
            tolerance_seconds: Clock skew tolerance in seconds (default 30s)

        Returns:
            Tuple of (lower_bound, upper_bound) datetimes
        """
        current = self.now()
        delta = timedelta(seconds=tolerance_seconds)
        return (current - delta, current + delta)


class SystemTimeProvider(TimeProvider):
    """
    Production time provider using system clock.

    This is the default provider for production use.
    """

    def __init__(self, default_timezone: str | None = None):
        """
        Initialize the system time provider.

        Args:
            default_timezone: Default timezone name (e.g., "Asia/Seoul")
                             Defaults to UTC if not specified.
        """
        self._timezone: tzinfo
        if default_timezone and default_timezone != "UTC":
            self._timezone = ZoneInfo(default_timezone)
        else:
            self._timezone = UTC

    def now(self) -> datetime:
        """Return current time in default timezone."""
        return datetime.now(self._timezone)

    def utcnow(self) -> datetime:
        """Return current UTC time."""
        return datetime.now(UTC)


class MockTimeProvider(TimeProvider):
    """
    Mock time provider for testing.

    Allows controlled time manipulation for deterministic tests.
    """

    def __init__(self, fixed_time: datetime | None = None):
        """
        Initialize mock time provider.

        Args:
            fixed_time: Fixed time to return. If None, uses current UTC time
                       as the starting point.
        """
        self._current_time = fixed_time or datetime.now(UTC)
        self._time_log: list[datetime] = [self._current_time]

    @property
    def current_time(self) -> datetime:
        """Get the current mock time."""
        return self._current_time

    @property
    def time_log(self) -> list[datetime]:
        """Get log of all times that were set."""
        return list(self._time_log)

    def now(self) -> datetime:
        """Return the fixed/mocked current time."""
        return self._current_time

    def utcnow(self) -> datetime:
        """Return the fixed/mocked UTC time."""
        if self._current_time.tzinfo is None:
            return self._current_time.replace(tzinfo=UTC)
        return self._current_time.astimezone(UTC)

    def set_time(self, new_time: datetime) -> None:
        """
        Set a new fixed time.

        Args:
            new_time: The new time to set
        """
        if new_time.tzinfo is None:
            new_time = new_time.replace(tzinfo=UTC)
        self._current_time = new_time
        self._time_log.append(new_time)

    def advance(self, delta: timedelta) -> datetime:
        """
        Advance time by a specified delta.

        Args:
            delta: Time delta to advance by

        Returns:
            The new current time
        """
        self._current_time = self._current_time + delta
        self._time_log.append(self._current_time)
        return self._current_time

    def rewind(self, delta: timedelta) -> datetime:
        """
        Rewind time by a specified delta.

        Args:
            delta: Time delta to rewind by

        Returns:
            The new current time
        """
        self._current_time = self._current_time - delta
        self._time_log.append(self._current_time)
        return self._current_time

    def simulate_clock_skew(self, skew_seconds: float) -> datetime:
        """
        Simulate clock drift by specified seconds.

        Positive values simulate a clock running ahead,
        negative values simulate a clock running behind.

        Args:
            skew_seconds: Seconds to drift (positive=ahead, negative=behind)

        Returns:
            The new current time after drift
        """
        return self.advance(timedelta(seconds=skew_seconds))

    def freeze(self) -> FrozenTime:
        """
        Create a context manager that freezes time.

        Returns:
            FrozenTime context manager
        """
        return FrozenTime(self)

    def reset(self) -> None:
        """Reset to current system time and clear log."""
        self._current_time = datetime.now(UTC)
        self._time_log = [self._current_time]


class FrozenTime:
    """Context manager for freezing time during a test block."""

    def __init__(self, provider: MockTimeProvider):
        self._provider = provider
        self._original_time: datetime | None = None

    def __enter__(self) -> MockTimeProvider:
        self._original_time = self._provider.current_time
        return self._provider

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if self._original_time is not None:
            self._provider.set_time(self._original_time)


# Global time provider instance
_time_provider: TimeProvider = SystemTimeProvider()


def get_time_provider() -> TimeProvider:
    """Get the current global time provider."""
    return _time_provider


def set_time_provider(provider: TimeProvider) -> None:
    """
    Set the global time provider.

    Useful for testing or custom time handling.

    Args:
        provider: The TimeProvider instance to use
    """
    global _time_provider
    _time_provider = provider


def reset_time_provider() -> None:
    """Reset to the default SystemTimeProvider."""
    global _time_provider
    _time_provider = SystemTimeProvider()


# Convenience functions using the global provider
def now() -> datetime:
    """Get current time from the global time provider."""
    return _time_provider.now()


def utcnow() -> datetime:
    """Get current UTC time from the global time provider."""
    return _time_provider.utcnow()


def is_within_clock_skew(
    timestamp: datetime,
    tolerance_seconds: float = 5.0,
) -> bool:
    """
    Check if a timestamp is within acceptable clock skew.

    Args:
        timestamp: Timestamp to check
        tolerance_seconds: Tolerance in seconds (default 5.0)

    Returns:
        True if within tolerance
    """
    return _time_provider.is_within_tolerance(
        timestamp,
        timedelta(seconds=tolerance_seconds),
    )
