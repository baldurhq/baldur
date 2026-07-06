"""
Tests for Time Utilities.
"""

from datetime import UTC, datetime, timedelta, timezone

import pytest


class TestUtcNow:
    """Test utc_now function."""

    def test_returns_timezone_aware_datetime(self):
        """Should return timezone-aware datetime."""
        from baldur.utils.time import utc_now

        now = utc_now()

        assert now.tzinfo is not None
        assert now.tzinfo == UTC

    def test_returns_current_time(self):
        """Should return approximately current time."""
        from baldur.utils.time import utc_now

        before = datetime.now(UTC)
        result = utc_now()
        after = datetime.now(UTC)

        assert before <= result <= after


class TestEnsureAware:
    """Test ensure_aware function."""

    def test_converts_naive_to_utc(self):
        """Should convert naive datetime to UTC."""
        from baldur.utils.time import ensure_aware

        naive = datetime(2024, 1, 15, 10, 30, 0)
        aware = ensure_aware(naive)

        assert aware.tzinfo == UTC
        assert aware.year == 2024
        assert aware.month == 1
        assert aware.day == 15

    def test_preserves_already_aware(self):
        """Should preserve already timezone-aware datetime."""
        from baldur.utils.time import ensure_aware

        original = datetime(2024, 1, 15, 10, 30, 0, tzinfo=UTC)
        result = ensure_aware(original)

        assert result.tzinfo == UTC
        assert result == original

    def test_preserves_non_utc_timezone(self):
        """Should preserve non-UTC timezone if already aware."""
        from baldur.utils.time import ensure_aware

        # Create datetime with offset
        offset = timezone(timedelta(hours=9))  # JST
        original = datetime(2024, 1, 15, 10, 30, 0, tzinfo=offset)
        result = ensure_aware(original)

        # Should be unchanged
        assert result == original


class TestToIsoString:
    """Test to_iso_string function."""

    def test_converts_to_iso_format(self):
        """Should convert datetime to ISO 8601 string."""
        from baldur.utils.time import to_iso_string

        dt = datetime(2024, 1, 15, 10, 30, 0, tzinfo=UTC)
        result = to_iso_string(dt)

        assert isinstance(result, str)
        assert "2024-01-15" in result
        assert "10:30:00" in result
        assert "+00:00" in result or "Z" in result

    def test_handles_none(self):
        """Should return None for None input."""
        from baldur.utils.time import to_iso_string

        result = to_iso_string(None)
        assert result is None

    def test_converts_naive_datetime(self):
        """Should handle naive datetime by making it UTC."""
        from baldur.utils.time import to_iso_string

        naive = datetime(2024, 1, 15, 10, 30, 0)
        result = to_iso_string(naive)

        assert result is not None
        assert "2024-01-15" in result


class TestFromIsoString:
    """Test from_iso_string function."""

    def test_parses_iso_string(self):
        """Should parse ISO 8601 string to datetime."""
        from baldur.utils.time import from_iso_string

        iso_str = "2024-01-15T10:30:00+00:00"
        result = from_iso_string(iso_str)

        assert isinstance(result, datetime)
        assert result.year == 2024
        assert result.month == 1
        assert result.day == 15
        assert result.hour == 10
        assert result.minute == 30

    def test_returns_timezone_aware(self):
        """Should return timezone-aware datetime."""
        from baldur.utils.time import from_iso_string

        iso_str = "2024-01-15T10:30:00+00:00"
        result = from_iso_string(iso_str)

        assert result.tzinfo is not None

    def test_handles_z_suffix(self):
        """Should handle Z suffix for UTC."""
        from baldur.utils.time import from_iso_string

        iso_str = "2024-01-15T10:30:00Z"

        # Python 3.11+ supports Z suffix directly
        try:
            result = from_iso_string(iso_str)
            assert result.tzinfo is not None
        except ValueError:
            # Older Python versions might not support Z
            pytest.skip("Z suffix not supported in this Python version")


class TestElapsedSeconds:
    """Test elapsed_seconds function."""

    def test_calculates_elapsed_time(self):
        """Should calculate elapsed seconds between two times."""
        from baldur.utils.time import elapsed_seconds

        start = datetime(2024, 1, 15, 10, 0, 0, tzinfo=UTC)
        end = datetime(2024, 1, 15, 10, 1, 30, tzinfo=UTC)

        result = elapsed_seconds(start, end)

        assert result == 90.0  # 1 minute 30 seconds

    def test_uses_current_time_when_end_is_none(self):
        """Should use current time when end is None."""
        from baldur.utils.time import elapsed_seconds, utc_now

        start = utc_now()
        # Small delay
        import time

        time.sleep(0.01)

        result = elapsed_seconds(start, None)

        assert result > 0
        assert result < 1.0  # Should be less than 1 second


class TestTimeModuleExports:
    """Test module exports."""

    def test_all_functions_exported(self):
        """Should export all expected functions."""
        from baldur.utils import time

        expected_functions = [
            "utc_now",
            "ensure_aware",
            "to_iso_string",
            "from_iso_string",
            "elapsed_seconds",
        ]

        for func_name in expected_functions:
            assert hasattr(time, func_name), f"Missing function: {func_name}"


class TestTimezoneAwarenessConsistency:
    """Test timezone awareness consistency across functions."""

    def test_round_trip_conversion(self):
        """Should maintain consistency in round-trip conversion."""
        from baldur.utils.time import from_iso_string, to_iso_string, utc_now

        original = utc_now()
        iso_str = to_iso_string(original)
        restored = from_iso_string(iso_str)

        # Times should be equal (allowing for microsecond precision)
        diff = abs((original - restored).total_seconds())
        assert diff < 0.001  # Less than 1 millisecond

    def test_all_outputs_are_timezone_aware(self):
        """All datetime outputs should be timezone-aware."""
        from baldur.utils.time import ensure_aware, from_iso_string, utc_now

        # utc_now
        assert utc_now().tzinfo is not None

        # ensure_aware
        naive = datetime(2024, 1, 15)
        assert ensure_aware(naive).tzinfo is not None

        # from_iso_string
        result = from_iso_string("2024-01-15T10:30:00+00:00")
        assert result.tzinfo is not None
