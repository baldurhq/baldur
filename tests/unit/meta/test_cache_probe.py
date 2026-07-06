"""
PrecomputedCacheProbe unit tests.

Covers:
- Contract: component_name, judgment matrix status/reason values
- Behavior: state transition (7 branches), ImportError handling, staleness boundary
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from baldur.meta.cache_probe import PrecomputedCacheProbe
from baldur.meta.health_probe import HealthStatus


def _make_passive_health(
    running: bool = True,
    registered_keys: list[str] | None = None,
    last_refresh_at: str | None = None,
    started_at: str | None = None,
    refresh_interval_seconds: float = 60.0,
    effective_interval_seconds: float | None = None,
) -> dict:
    return {
        "running": running,
        "registered_keys": registered_keys or [],
        "last_refresh_at": last_refresh_at,
        "started_at": started_at,
        "refresh_interval_seconds": refresh_interval_seconds,
        "effective_interval_seconds": (
            effective_interval_seconds
            if effective_interval_seconds is not None
            else refresh_interval_seconds
        ),
    }


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _utc_now() -> datetime:
    return datetime.now(UTC)


@pytest.fixture
def probe():
    return PrecomputedCacheProbe()


@pytest.fixture
def mock_settings():
    settings = MagicMock()
    settings.probe_cache_staleness_multiplier = 2.0
    return settings


class TestPrecomputedCacheProbeContract:
    """Contract verification for PrecomputedCacheProbe design values."""

    def test_component_name_is_precomputed_cache(self, probe):
        """component_name must be 'precomputed_cache' per 411 ED1."""
        assert probe.component_name == "precomputed_cache"

    def test_worker_not_running_returns_unhealthy_with_reason(
        self, probe, mock_settings
    ):
        """Not running → UNHEALTHY with exact reason string."""
        now = _utc_now()
        health = _make_passive_health(running=False)

        with (
            patch(
                "baldur.services.precomputed_cache.worker.get_precomputed_cache_worker"
            ) as mock_worker_fn,
            patch(
                "baldur.meta.config.get_meta_watchdog_settings",
                return_value=mock_settings,
            ),
            patch("baldur.meta.cache_probe.utc_now", return_value=now),
        ):
            mock_worker_fn.return_value.get_passive_health.return_value = health
            result = probe.probe()

        assert result.status == HealthStatus.UNHEALTHY
        assert result.reason == "Cache worker not running"

    def test_cold_start_within_threshold_returns_unknown(self, probe, mock_settings):
        """Cold start (no refresh yet, started recently) → UNKNOWN."""
        now = _utc_now()
        started = now - timedelta(seconds=30)  # 30s < 2.0 * 60 = 120s
        health = _make_passive_health(
            running=True,
            registered_keys=["k1"],
            started_at=_iso(started),
            refresh_interval_seconds=60.0,
        )

        with (
            patch(
                "baldur.services.precomputed_cache.worker.get_precomputed_cache_worker"
            ) as mock_worker_fn,
            patch(
                "baldur.meta.config.get_meta_watchdog_settings",
                return_value=mock_settings,
            ),
            patch("baldur.meta.cache_probe.utc_now", return_value=now),
        ):
            mock_worker_fn.return_value.get_passive_health.return_value = health
            result = probe.probe()

        assert result.status == HealthStatus.UNKNOWN
        assert "first refresh not yet completed" in result.reason

    def test_cold_start_overdue_returns_unhealthy(self, probe, mock_settings):
        """Cold start exceeding threshold → UNHEALTHY (deadlock)."""
        now = _utc_now()
        started = now - timedelta(seconds=200)  # 200s > 2.0 * 60 = 120s
        health = _make_passive_health(
            running=True,
            registered_keys=["k1"],
            started_at=_iso(started),
            refresh_interval_seconds=60.0,
        )

        with (
            patch(
                "baldur.services.precomputed_cache.worker.get_precomputed_cache_worker"
            ) as mock_worker_fn,
            patch(
                "baldur.meta.config.get_meta_watchdog_settings",
                return_value=mock_settings,
            ),
            patch("baldur.meta.cache_probe.utc_now", return_value=now),
        ):
            mock_worker_fn.return_value.get_passive_health.return_value = health
            result = probe.probe()

        assert result.status == HealthStatus.UNHEALTHY
        assert "deadlock" in result.reason.lower()

    def test_no_compute_functions_returns_degraded(self, probe, mock_settings):
        """Running but no functions → DEGRADED."""
        now = _utc_now()
        refreshed = now - timedelta(seconds=10)
        health = _make_passive_health(
            running=True,
            registered_keys=[],
            last_refresh_at=_iso(refreshed),
            started_at=_iso(now - timedelta(minutes=5)),
            refresh_interval_seconds=60.0,
        )

        with (
            patch(
                "baldur.services.precomputed_cache.worker.get_precomputed_cache_worker"
            ) as mock_worker_fn,
            patch(
                "baldur.meta.config.get_meta_watchdog_settings",
                return_value=mock_settings,
            ),
            patch("baldur.meta.cache_probe.utc_now", return_value=now),
        ):
            mock_worker_fn.return_value.get_passive_health.return_value = health
            result = probe.probe()

        assert result.status == HealthStatus.DEGRADED
        assert "no compute functions" in result.reason

    def test_import_error_returns_unknown(self, probe):
        """ImportError → UNKNOWN with error string."""
        with patch(
            "baldur.services.precomputed_cache.worker.get_precomputed_cache_worker",
            side_effect=ImportError("no module"),
        ):
            result = probe.probe()

        assert result.status == HealthStatus.UNKNOWN
        assert result.error == "precomputed_cache module not available"


class TestPrecomputedCacheProbeBehavior:
    """Behavior verification for judgment logic."""

    def test_healthy_when_running_and_fresh(self, probe, mock_settings):
        """Running + keys + recent refresh → HEALTHY."""
        now = _utc_now()
        refreshed = now - timedelta(seconds=30)  # 30/60 = 0.5x < 2.0x
        health = _make_passive_health(
            running=True,
            registered_keys=["stats", "dashboard"],
            last_refresh_at=_iso(refreshed),
            started_at=_iso(now - timedelta(minutes=10)),
            refresh_interval_seconds=60.0,
        )

        with (
            patch(
                "baldur.services.precomputed_cache.worker.get_precomputed_cache_worker"
            ) as mock_worker_fn,
            patch(
                "baldur.meta.config.get_meta_watchdog_settings",
                return_value=mock_settings,
            ),
            patch("baldur.meta.cache_probe.utc_now", return_value=now),
        ):
            mock_worker_fn.return_value.get_passive_health.return_value = health
            result = probe.probe()

        assert result.status == HealthStatus.HEALTHY
        assert result.reason == ""
        assert result.details["staleness_ratio"] == pytest.approx(0.5, abs=0.1)

    def test_stale_cache_returns_degraded(self, probe, mock_settings):
        """Staleness ratio > multiplier → DEGRADED."""
        now = _utc_now()
        refreshed = now - timedelta(seconds=150)  # 150/60 = 2.5x > 2.0x
        health = _make_passive_health(
            running=True,
            registered_keys=["k1"],
            last_refresh_at=_iso(refreshed),
            started_at=_iso(now - timedelta(minutes=10)),
            refresh_interval_seconds=60.0,
        )

        with (
            patch(
                "baldur.services.precomputed_cache.worker.get_precomputed_cache_worker"
            ) as mock_worker_fn,
            patch(
                "baldur.meta.config.get_meta_watchdog_settings",
                return_value=mock_settings,
            ),
            patch("baldur.meta.cache_probe.utc_now", return_value=now),
        ):
            mock_worker_fn.return_value.get_passive_health.return_value = health
            result = probe.probe()

        assert result.status == HealthStatus.DEGRADED
        assert "stale" in result.reason.lower()

    def test_staleness_boundary_at_exact_multiplier_is_healthy(
        self, probe, mock_settings
    ):
        """Staleness ratio == multiplier (not >) → HEALTHY (boundary)."""
        now = _utc_now()
        # Exactly 2.0x — should be HEALTHY (> not >=)
        refreshed = now - timedelta(seconds=120)  # 120/60 = 2.0x
        health = _make_passive_health(
            running=True,
            registered_keys=["k1"],
            last_refresh_at=_iso(refreshed),
            started_at=_iso(now - timedelta(minutes=10)),
            refresh_interval_seconds=60.0,
        )

        with (
            patch(
                "baldur.services.precomputed_cache.worker.get_precomputed_cache_worker"
            ) as mock_worker_fn,
            patch(
                "baldur.meta.config.get_meta_watchdog_settings",
                return_value=mock_settings,
            ),
            patch("baldur.meta.cache_probe.utc_now", return_value=now),
        ):
            mock_worker_fn.return_value.get_passive_health.return_value = health
            result = probe.probe()

        assert result.status == HealthStatus.HEALTHY

    def test_cold_start_boundary_at_exact_threshold_is_unknown(
        self, probe, mock_settings
    ):
        """Cold start elapsed == threshold → UNKNOWN (not UNHEALTHY, > not >=)."""
        now = _utc_now()
        # Exactly at threshold: 120s == 2.0 * 60
        started = now - timedelta(seconds=120)
        health = _make_passive_health(
            running=True,
            registered_keys=["k1"],
            started_at=_iso(started),
            refresh_interval_seconds=60.0,
        )

        with (
            patch(
                "baldur.services.precomputed_cache.worker.get_precomputed_cache_worker"
            ) as mock_worker_fn,
            patch(
                "baldur.meta.config.get_meta_watchdog_settings",
                return_value=mock_settings,
            ),
            patch("baldur.meta.cache_probe.utc_now", return_value=now),
        ):
            mock_worker_fn.return_value.get_passive_health.return_value = health
            result = probe.probe()

        assert result.status == HealthStatus.UNKNOWN

    def test_result_details_contain_expected_keys(self, probe, mock_settings):
        """Probe result details must contain all documented keys."""
        now = _utc_now()
        refreshed = now - timedelta(seconds=10)
        health = _make_passive_health(
            running=True,
            registered_keys=["k1"],
            last_refresh_at=_iso(refreshed),
            started_at=_iso(now - timedelta(minutes=5)),
            refresh_interval_seconds=60.0,
        )

        with (
            patch(
                "baldur.services.precomputed_cache.worker.get_precomputed_cache_worker"
            ) as mock_worker_fn,
            patch(
                "baldur.meta.config.get_meta_watchdog_settings",
                return_value=mock_settings,
            ),
            patch("baldur.meta.cache_probe.utc_now", return_value=now),
        ):
            mock_worker_fn.return_value.get_passive_health.return_value = health
            result = probe.probe()

        expected_keys = {
            "running",
            "registered_keys",
            "last_refresh_at",
            "started_at",
            "refresh_interval_seconds",
            "staleness_ratio",
        }
        assert expected_keys == set(result.details.keys())

    def test_general_exception_returns_unknown_with_error(self, probe):
        """Unexpected exception → UNKNOWN with error message."""
        with patch(
            "baldur.services.precomputed_cache.worker.get_precomputed_cache_worker",
            side_effect=RuntimeError("unexpected"),
        ):
            result = probe.probe()

        assert result.status == HealthStatus.UNKNOWN
        assert "unexpected" in result.error

    def test_cold_start_without_started_at_returns_unknown(self, probe, mock_settings):
        """Running but no started_at and no refresh → UNKNOWN."""
        now = _utc_now()
        health = _make_passive_health(
            running=True,
            registered_keys=["k1"],
        )

        with (
            patch(
                "baldur.services.precomputed_cache.worker.get_precomputed_cache_worker"
            ) as mock_worker_fn,
            patch(
                "baldur.meta.config.get_meta_watchdog_settings",
                return_value=mock_settings,
            ),
            patch("baldur.meta.cache_probe.utc_now", return_value=now),
        ):
            mock_worker_fn.return_value.get_passive_health.return_value = health
            result = probe.probe()

        assert result.status == HealthStatus.UNKNOWN
        assert "first refresh not yet completed" in result.reason


class TestCacheProbeEffectiveIntervalBehavior:
    """Behavior verification for effective_interval_seconds usage (doc 445 D8)."""

    def test_backoff_expands_staleness_threshold(self, probe, mock_settings):
        """When effective_interval > refresh_interval, staleness threshold expands."""
        now = _utc_now()
        # 150s elapsed, refresh_interval=60, effective_interval=120 (backoff)
        # staleness_ratio = 150/120 = 1.25x, threshold = 2.0 * 120 = 240s
        # 1.25 < 2.0 → HEALTHY (without effective_interval: 150/60=2.5 > 2.0 → DEGRADED)
        refreshed = now - timedelta(seconds=150)
        health = _make_passive_health(
            running=True,
            registered_keys=["k1"],
            last_refresh_at=_iso(refreshed),
            started_at=_iso(now - timedelta(minutes=10)),
            refresh_interval_seconds=60.0,
            effective_interval_seconds=120.0,
        )

        with (
            patch(
                "baldur.services.precomputed_cache.worker.get_precomputed_cache_worker"
            ) as mock_worker_fn,
            patch(
                "baldur.meta.config.get_meta_watchdog_settings",
                return_value=mock_settings,
            ),
            patch("baldur.meta.cache_probe.utc_now", return_value=now),
        ):
            mock_worker_fn.return_value.get_passive_health.return_value = health
            result = probe.probe()

        assert result.status == HealthStatus.HEALTHY

    def test_fallback_to_refresh_interval_when_key_missing(self, probe, mock_settings):
        """Missing effective_interval_seconds key falls back to refresh_interval."""
        now = _utc_now()
        refreshed = now - timedelta(seconds=30)
        health = {
            "running": True,
            "registered_keys": ["k1"],
            "last_refresh_at": _iso(refreshed),
            "started_at": _iso(now - timedelta(minutes=10)),
            "refresh_interval_seconds": 60.0,
        }

        with (
            patch(
                "baldur.services.precomputed_cache.worker.get_precomputed_cache_worker"
            ) as mock_worker_fn,
            patch(
                "baldur.meta.config.get_meta_watchdog_settings",
                return_value=mock_settings,
            ),
            patch("baldur.meta.cache_probe.utc_now", return_value=now),
        ):
            mock_worker_fn.return_value.get_passive_health.return_value = health
            result = probe.probe()

        assert result.status == HealthStatus.HEALTHY
