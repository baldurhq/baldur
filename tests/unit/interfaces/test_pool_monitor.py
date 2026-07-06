"""NoOpPoolStatsProvider + interface unit tests (516 D2).

Scope:
- ``NoOpPoolStatsProvider`` returns an empty ``PoolStats`` so callers never
  receive ``None`` when PRO is absent.
- ``PoolStats`` derived properties (``usage_percent`` boundary, ``is_exhausted``)
  exercised against the NoOp default.
- ``PoolHealthStatus`` enum surface and JSON-serializability contract.
- ``PoolStatsProvider`` ABC enforces ``get_stats``.
"""

from __future__ import annotations

from abc import ABC

import pytest

from baldur.interfaces.pool_monitor import (
    NoOpPoolStatsProvider,
    PoolHealthStatus,
    PoolStats,
    PoolStatsProvider,
)

# =============================================================================
# NoOpPoolStatsProvider — fail-safe default
# =============================================================================


class TestNoOpPoolStatsProviderBehavior:
    """NoOpPoolStatsProvider returns an empty PoolStats — zero everywhere."""

    def test_get_stats_returns_zero_capacity(self):
        provider = NoOpPoolStatsProvider()

        stats = provider.get_stats()

        assert stats.max_connections == 0
        assert stats.active_connections == 0
        assert stats.available_connections == 0
        assert stats.waiting_requests == 0

    def test_default_pool_name_is_noop(self):
        provider = NoOpPoolStatsProvider()

        assert provider.get_stats().pool_name == "noop"

    def test_custom_pool_name_propagates(self):
        provider = NoOpPoolStatsProvider(pool_name="custom")

        assert provider.get_stats().pool_name == "custom"

    def test_get_stats_returns_fresh_dataclass_each_call(self):
        """Each call returns a fresh ``PoolStats`` so callers can mutate the
        returned dataclass (it isn't frozen) without leaking into subsequent
        snapshots.
        """
        provider = NoOpPoolStatsProvider()

        first = provider.get_stats()
        second = provider.get_stats()

        assert first is not second


# =============================================================================
# PoolStats — derived properties at boundary
# =============================================================================


class TestPoolStatsDerivedBehavior:
    """``usage_percent`` and ``is_exhausted`` boundary behavior."""

    def test_usage_percent_zero_capacity_returns_zero(self):
        """max_connections == 0 must NOT raise ZeroDivisionError — return 0."""
        stats = PoolStats(
            pool_name="empty",
            max_connections=0,
            active_connections=0,
            available_connections=0,
        )

        assert stats.usage_percent == 0.0

    def test_usage_percent_half_capacity(self):
        stats = PoolStats(
            pool_name="half",
            max_connections=10,
            active_connections=5,
            available_connections=5,
        )

        assert stats.usage_percent == 50.0

    def test_usage_percent_full_capacity(self):
        stats = PoolStats(
            pool_name="full",
            max_connections=10,
            active_connections=10,
            available_connections=0,
        )

        assert stats.usage_percent == 100.0

    def test_is_exhausted_true_when_available_zero_and_active_at_max(self):
        stats = PoolStats(
            pool_name="exhausted",
            max_connections=4,
            active_connections=4,
            available_connections=0,
        )

        assert stats.is_exhausted is True

    def test_is_exhausted_false_when_available_above_zero(self):
        stats = PoolStats(
            pool_name="not_exhausted",
            max_connections=4,
            active_connections=4,
            available_connections=1,
        )

        assert stats.is_exhausted is False

    def test_is_exhausted_false_when_under_max(self):
        stats = PoolStats(
            pool_name="under_max",
            max_connections=4,
            active_connections=3,
            available_connections=0,
        )

        assert stats.is_exhausted is False


# =============================================================================
# PoolHealthStatus — enum surface
# =============================================================================


class TestPoolHealthStatusContract:
    """PoolHealthStatus values must match the design contract.

    The enum is consumed by ``baldur_pro``'s ``ConnectionPoolMonitor`` and by
    OSS dashboards via the value strings, so the wire representation is
    load-bearing.
    """

    @pytest.mark.parametrize(
        ("member", "expected_value"),
        [
            (PoolHealthStatus.HEALTHY, "healthy"),
            (PoolHealthStatus.WARNING, "warning"),
            (PoolHealthStatus.CRITICAL, "critical"),
            (PoolHealthStatus.EXHAUSTED, "exhausted"),
            (PoolHealthStatus.LEAK_SUSPECTED, "leak_suspected"),
            (PoolHealthStatus.UNKNOWN, "unknown"),
        ],
    )
    def test_member_value(self, member, expected_value):
        assert member.value == expected_value

    def test_pool_health_status_is_str_subclass(self):
        """``str`` subclass guarantees JSON serializability without a custom encoder."""
        assert isinstance(PoolHealthStatus.HEALTHY, str)

    def test_member_count_matches_design_document(self):
        assert len(PoolHealthStatus) == 6


# =============================================================================
# PoolStatsProvider — ABC enforcement
# =============================================================================


class TestPoolStatsProviderContract:
    """PoolStatsProvider is an ABC — get_stats must be implemented."""

    def test_provider_is_abc(self):
        assert issubclass(PoolStatsProvider, ABC)

    def test_incomplete_subclass_cannot_instantiate(self):
        class _Incomplete(PoolStatsProvider):
            pass

        with pytest.raises(TypeError, match="abstract"):
            _Incomplete()

    def test_complete_subclass_instantiates(self):
        class _Complete(PoolStatsProvider):
            def get_stats(self) -> PoolStats:
                return PoolStats(
                    pool_name="complete",
                    max_connections=1,
                    active_connections=0,
                    available_connections=1,
                )

        instance = _Complete()
        stats = instance.get_stats()
        assert stats.pool_name == "complete"
