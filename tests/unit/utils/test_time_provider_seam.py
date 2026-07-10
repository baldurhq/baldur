"""Seam test: baldur.utils.time.utc_now() reads through the global TimeProvider (D7).

Migrated from test_clock_skew.py's TestTimezoneIntegration cases and retargeted at the
canonical utc_now() (the parallel now-module was removed). reset_time_provider() runs in a
finally block so a failing test cannot leak a MockTimeProvider into other xdist workers.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from baldur.core.time_provider import (
    MockTimeProvider,
    reset_time_provider,
    set_time_provider,
)
from baldur.utils.time import utc_now


class TestUtcNowHonorsTimeProvider:
    def test_utc_now_uses_global_time_provider(self):
        fixed = datetime(2024, 12, 25, 12, 0, 0, tzinfo=UTC)
        try:
            set_time_provider(MockTimeProvider(fixed_time=fixed))
            assert utc_now() == fixed
        finally:
            reset_time_provider()

    def test_utc_now_reflects_provider_advance(self):
        start = datetime(2024, 1, 1, 0, 0, 0, tzinfo=UTC)
        provider = MockTimeProvider(fixed_time=start)
        try:
            set_time_provider(provider)
            assert utc_now() == start
            provider.advance(timedelta(hours=3))
            assert utc_now() == start + timedelta(hours=3)
        finally:
            reset_time_provider()

    def test_default_provider_returns_aware_utc(self):
        reset_time_provider()
        result = utc_now()
        assert result.tzinfo is not None
        assert result.utcoffset() == timedelta(0)
