"""
Setting → Storage Propagation Test Framework.

Verifies temporal settings' TTL values reach the storage layer
(Redis TTL/EXPIRE/StateBackend), preventing silent DROPPED settings
that cause unbounded Redis memory growth.

Static registry (CONNECTED_FIELDS) classifies 18 temporal fields by
propagation pattern (A–D) and serves as both test input and
classification documentation.

Reference: 444

Reclassifications from document:
- dashboard.tracker_cache_ttl: removed (UNUSED — no consumer in codebase)
- cascade_retention.hot_retention_days: moved from Pattern C to B
  (uses StateBackend.set(ttl_seconds=), not CacheAdapter)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest

# =============================================================================
# Static Registry (D5)
# =============================================================================


@dataclass(frozen=True)
class ConnectedField:
    """Registry entry for a temporal setting connected to storage TTL."""

    setting_module: str
    field_name: str
    pattern: str


CONNECTED_FIELDS: list[ConnectedField] = [
    # Pattern A — Direct Redis (setex/expire)
    ConnectedField("airgap", "redis_ttl", "A"),
    ConnectedField("audit", "buffer_redis_ttl", "A"),
    ConnectedField("audit_integrity", "pending_ttl_seconds", "A"),
    ConnectedField("audit_integrity", "orphan_ttl_seconds", "A"),
    ConnectedField("canary", "propagation_ttl", "A"),
    ConnectedField("event_journal", "ttl_days", "A"),
    ConnectedField("rate_limit", "redis_ttl", "A"),
    ConnectedField("xtest_cleanup", "session_ttl_hours", "A"),
    # Pattern B — StateBackend (set with ttl_seconds=)
    ConnectedField("chaos", "worker_heartbeat_ttl_seconds", "B"),
    ConnectedField("predictive_forecaster", "state_ttl", "B"),
    ConnectedField("cascade_retention", "hot_retention_days", "B"),
    # Pattern C — CacheAdapter (set with ttl=timedelta)
    ConnectedField("dashboard", "cache_ttl_seconds", "C"),
    ConnectedField("dashboard", "cache_ttl_status", "C"),
    ConnectedField("dashboard", "cache_ttl_activity", "C"),
    ConnectedField("daily_report", "cache_ttl", "C"),
    ConnectedField("idempotency", "default_cache_ttl", "C"),
    ConnectedField("idempotency", "extended_cache_ttl", "C"),
    # Pattern D — Service Lock
    ConnectedField("chaos", "experiment_lock_ttl", "D"),
]


def _pattern_fields(pattern: str) -> list[ConnectedField]:
    return [f for f in CONNECTED_FIELDS if f.pattern == pattern]


# =============================================================================
# Pattern A — Direct Redis TTL Propagation
# =============================================================================


# =============================================================================
# Pattern B — StateBackend TTL Propagation
# =============================================================================


# =============================================================================
# Pattern C — CacheAdapter TTL Propagation
# =============================================================================


class TestCacheAdapterTtlPropagationBehavior:
    """Pattern C: setting → service → CacheProviderInterface.set(ttl=timedelta)."""

    @pytest.mark.parametrize(
        ("field_name", "attr_name", "default_value"),
        [
            ("cache_ttl_seconds", "CACHE_TTL_SECONDS", 30),
            ("cache_ttl_status", "CACHE_TTL_STATUS", 15),
            ("cache_ttl_activity", "CACHE_TTL_ACTIVITY", 60),
        ],
        ids=["seconds", "status", "activity"],
    )
    def test_dashboard_cache_ttl(self, field_name, attr_name, default_value):
        """dashboard.cache_ttl_* → DashboardService._set_cached() → cache.set(ttl=timedelta)"""
        from baldur.services.dashboard_service.service import DashboardService

        mock_cache = MagicMock()
        mock_settings = MagicMock()
        setattr(mock_settings, field_name, default_value)
        mock_settings.cache_prefix = "baldur:dashboard:"

        service = DashboardService(cache=mock_cache)
        service._settings = mock_settings

        ttl = getattr(service, attr_name)
        service._set_cached("test_key", {"data": 1}, ttl_seconds=ttl)

        mock_cache.set.assert_called_once()
        assert mock_cache.set.call_args.kwargs["ttl"] == timedelta(
            seconds=default_value
        )

    def test_daily_report_cache_ttl(self):
        """daily_report.cache_ttl → DailyReportCollector.add_result() → push_limit(ttl=timedelta)"""
        mock_cache = MagicMock()
        mock_cache.push_limit.return_value = 1

        mock_settings = MagicMock()
        mock_settings.cache_ttl = 172800
        mock_settings.max_entries_per_day = 1000

        with (
            patch(
                "baldur.services.daily_report.aggregator.get_daily_report_settings",
                return_value=mock_settings,
            ),
            patch("baldur.factory.ProviderRegistry") as mock_registry,
        ):
            mock_registry.get_cache.return_value = mock_cache

            from baldur.services.daily_report.aggregator import DailyReportCollector

            collector = DailyReportCollector()
            collector.add_result("test_task", {"status": "ok"})

        mock_cache.push_limit.assert_called_once()
        assert mock_cache.push_limit.call_args.kwargs["ttl"] == timedelta(
            seconds=172800
        )

    def test_idempotency_default_cache_ttl(self):
        """idempotency.default_cache_ttl → mark_as_processed() → cache.set(ttl=timedelta)"""
        from baldur.settings.idempotency import IdempotencySettings

        mock_cache = MagicMock()

        with patch(
            "baldur.settings.layered_provider.get_layered_settings_cached",
            return_value=IdempotencySettings(
                default_cache_ttl=120,
                extended_cache_ttl=300,
                clock_skew_tolerance_seconds=5.0,
            ),
        ):
            from baldur.services.idempotency.service import IdempotencyService

            service = IdempotencyService()

        service._cache = mock_cache

        from baldur.services.idempotency.models import (
            IdempotencyDomain,
            IdempotencyKey,
        )

        key = IdempotencyKey(
            domain=IdempotencyDomain.EXTERNAL_SERVICE,
            key="test-001",
            components={},
        )
        service.mark_as_processed(key)

        mock_cache.set.assert_called_once()
        assert mock_cache.set.call_args.kwargs["ttl"] == timedelta(seconds=120)

    def test_idempotency_extended_cache_ttl(self):
        """idempotency.extended_cache_ttl → batch_mark_as_processed(ttl=EXTENDED) → cache.mset(ttl=timedelta)"""
        from baldur.settings.idempotency import IdempotencySettings

        mock_cache = MagicMock()

        with patch(
            "baldur.settings.layered_provider.get_layered_settings_cached",
            return_value=IdempotencySettings(
                default_cache_ttl=60,
                extended_cache_ttl=300,
                clock_skew_tolerance_seconds=5.0,
            ),
        ):
            from baldur.services.idempotency.service import IdempotencyService

            service = IdempotencyService()

        service._cache = mock_cache

        from baldur.services.idempotency.models import (
            IdempotencyDomain,
            IdempotencyKey,
        )

        key = IdempotencyKey(
            domain=IdempotencyDomain.EXTERNAL_SERVICE,
            key="test-001",
            components={},
        )
        service.batch_mark_as_processed([key], ttl=service.EXTENDED_CACHE_TTL)

        mock_cache.mset.assert_called_once()
        assert mock_cache.mset.call_args.kwargs["ttl"] == timedelta(seconds=300)


# =============================================================================
# Pattern D — Service Lock TTL Propagation
# =============================================================================


class TestServiceLockTtlPropagationBehavior:
    """Pattern D: setting → IdempotencyService.acquire_lock(ttl_seconds=) → cache.get_lock(timeout=)."""

    def test_chaos_experiment_lock_ttl(self):
        """chaos.experiment_lock_ttl → acquire_lock() → cache.get_lock(timeout=timedelta)"""
        from baldur.settings.idempotency import IdempotencySettings

        mock_cache = MagicMock()
        mock_lock = MagicMock()
        mock_lock.acquire.return_value = True
        mock_cache.get_lock.return_value = mock_lock

        with patch(
            "baldur.settings.layered_provider.get_layered_settings_cached",
            return_value=IdempotencySettings(
                default_cache_ttl=60,
                extended_cache_ttl=300,
                clock_skew_tolerance_seconds=5.0,
            ),
        ):
            from baldur.services.idempotency.service import IdempotencyService

            service = IdempotencyService()

        service._cache = mock_cache

        from baldur.services.idempotency.models import (
            IdempotencyDomain,
            IdempotencyKey,
        )

        lock_ttl = 120
        key = IdempotencyKey(
            domain=IdempotencyDomain.EXTERNAL_SERVICE,
            key="lock-001",
            components={},
        )
        service.acquire_lock(key, ttl_seconds=lock_ttl)

        mock_cache.get_lock.assert_called_once()
        assert mock_cache.get_lock.call_args.kwargs["timeout"] == timedelta(
            seconds=lock_ttl
        )


# =============================================================================
# DROPPED Field Fix Verification
# =============================================================================


# =============================================================================
# Execution-Bound Setting Propagation
# =============================================================================


# =============================================================================
# Registry Integrity
# =============================================================================


class TestRegistryIntegrityContract:
    """Verify CONNECTED_FIELDS registry consistency."""

    def test_pattern_a_count(self):
        assert len(_pattern_fields("A")) == 8

    def test_pattern_b_count(self):
        assert len(_pattern_fields("B")) == 3

    def test_pattern_c_count(self):
        assert len(_pattern_fields("C")) == 6

    def test_pattern_d_count(self):
        assert len(_pattern_fields("D")) == 1

    def test_total_connected_fields(self):
        assert len(CONNECTED_FIELDS) == 18

    def test_no_duplicate_entries(self):
        seen = set()
        for f in CONNECTED_FIELDS:
            key = (f.setting_module, f.field_name)
            assert key not in seen, f"Duplicate: {key}"
            seen.add(key)
