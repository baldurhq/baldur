"""
Tests for Time Utilities.
"""

from datetime import UTC, datetime, timedelta

from baldur.utils.time import (
    add_seconds,
    elapsed_seconds,
    ensure_aware,
    format_duration,
    from_iso_string,
    is_expired,
    to_iso_string,
    utc_now,
)


class TestUtcNow:
    """Tests for utc_now function."""

    def test_returns_datetime(self):
        """Should return datetime object."""
        now = utc_now()
        assert isinstance(now, datetime)

    def test_is_timezone_aware(self):
        """Should return timezone-aware datetime."""
        now = utc_now()
        assert now.tzinfo is not None
        assert now.tzinfo == UTC


class TestEnsureAware:
    """Tests for ensure_aware function."""

    def test_converts_naive_to_utc(self):
        """Should convert naive datetime to UTC."""
        naive = datetime(2024, 1, 15, 10, 30, 0)
        aware = ensure_aware(naive)

        assert aware.tzinfo == UTC
        assert aware.year == 2024
        assert aware.month == 1

    def test_preserves_aware_datetime(self):
        """Should preserve already aware datetime."""
        aware_input = datetime(2024, 1, 15, 10, 30, 0, tzinfo=UTC)
        result = ensure_aware(aware_input)

        assert result is aware_input


class TestToIsoString:
    """Tests for to_iso_string function."""

    def test_converts_to_iso_format(self):
        """Should convert to ISO 8601 format."""
        dt = datetime(2024, 1, 15, 10, 30, 0, tzinfo=UTC)
        iso = to_iso_string(dt)

        assert "2024-01-15" in iso
        assert "10:30:00" in iso
        assert "+00:00" in iso

    def test_returns_none_for_none(self):
        """Should return None for None input."""
        assert to_iso_string(None) is None

    def test_converts_naive_with_utc(self):
        """Should add UTC timezone to naive datetime."""
        naive = datetime(2024, 1, 15, 10, 30, 0)
        iso = to_iso_string(naive)

        assert "+00:00" in iso


class TestFromIsoString:
    """Tests for from_iso_string function."""

    def test_parses_iso_string(self):
        """Should parse ISO 8601 string."""
        iso = "2024-01-15T10:30:00+00:00"
        dt = from_iso_string(iso)

        assert dt.year == 2024
        assert dt.month == 1
        assert dt.day == 15
        assert dt.hour == 10
        assert dt.minute == 30

    def test_returns_timezone_aware(self):
        """Should return timezone-aware datetime."""
        iso = "2024-01-15T10:30:00+00:00"
        dt = from_iso_string(iso)

        assert dt.tzinfo is not None


class TestElapsedSeconds:
    """Tests for elapsed_seconds function."""

    def test_calculates_elapsed_time(self):
        """Should calculate elapsed seconds between two times."""
        start = datetime(2024, 1, 15, 10, 0, 0, tzinfo=UTC)
        end = datetime(2024, 1, 15, 10, 1, 30, tzinfo=UTC)

        elapsed = elapsed_seconds(start, end)

        assert elapsed == 90.0

    def test_uses_current_time_when_end_is_none(self):
        """Should use current time when end is None."""
        start = utc_now() - timedelta(seconds=5)
        elapsed = elapsed_seconds(start)

        assert 4.9 <= elapsed <= 6.0


class TestIsExpired:
    """Tests for is_expired function."""

    def test_returns_true_when_expired(self):
        """Should return True when TTL exceeded."""
        old_time = utc_now() - timedelta(hours=2)
        assert is_expired(old_time, ttl_seconds=3600) is True

    def test_returns_false_when_not_expired(self):
        """Should return False when TTL not exceeded."""
        recent_time = utc_now() - timedelta(minutes=30)
        assert is_expired(recent_time, ttl_seconds=3600) is False


class TestAddSeconds:
    """Tests for add_seconds function."""

    def test_adds_seconds_to_datetime(self):
        """Should add seconds to datetime."""
        start = datetime(2024, 1, 15, 10, 0, 0, tzinfo=UTC)
        result = add_seconds(start, 90)

        assert result.hour == 10
        assert result.minute == 1
        assert result.second == 30


class TestFormatDuration:
    """Tests for format_duration function."""

    def test_formats_seconds_only(self):
        """Should format short durations as seconds."""
        assert format_duration(45) == "45s"

    def test_formats_minutes_and_seconds(self):
        """Should format as minutes and seconds."""
        assert format_duration(125) == "2m 5s"

    def test_formats_hours_minutes_seconds(self):
        """Should format with hours."""
        result = format_duration(9015)  # 2h 30m 15s
        assert "2h" in result
        assert "30m" in result
        assert "15s" in result

    def test_formats_days(self):
        """Should format with days."""
        result = format_duration(90000)  # 1d 1h
        assert "1d" in result
        assert "1h" in result

    def test_formats_zero(self):
        """Should format zero duration."""
        assert format_duration(0) == "0s"
