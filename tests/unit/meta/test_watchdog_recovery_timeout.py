"""
391 — Watchdog Recovery Total Timeout unit tests.

Tests for: two-phase check_health, budget-aware recovery, TimeoutExecutor
integration, _attempt_guarded_recovery, _build_recovery_fn, component priority
ordering, item cap, stop_event cooperation, CB force_close bug fix.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from baldur.meta.config import MetaWatchdogSettings
from baldur.meta.health_probe import HealthProbeManager, HealthStatus, ProbeResult

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def settings():
    """Test settings with low threshold for quick recovery trigger.

    recovery_enabled=True (558 D7) keeps FULL recovery mode reachable; the v1.0
    default (False) short-circuits to slice-A escalate-only.
    """
    return MetaWatchdogSettings(
        enabled=True,
        self_cb_enabled=False,
        dry_run_mode=False,
        recovery_enabled=True,
        self_cb_failure_threshold=2,
        recovery_cooldown_seconds=300.0,
        recovery_total_timeout_seconds=60.0,
        max_items_per_recovery=5,
        k8s_api_timeout_seconds=30.0,
        probe_interval_seconds=30.0,
    )


@pytest.fixture
def mock_probe_manager():
    """Mock probe manager."""
    manager = MagicMock(spec=HealthProbeManager)
    manager.probe_all.return_value = {}
    manager.get_overall_status.return_value = HealthStatus.HEALTHY
    manager.get_last_results.return_value = {}
    return manager


def _make_probe(
    component: str,
    status: HealthStatus = HealthStatus.UNHEALTHY,
    **details: object,
) -> ProbeResult:
    return ProbeResult(
        component=component,
        status=status,
        latency_ms=1,
        timestamp=datetime.now(UTC),
        error="test error" if status == HealthStatus.UNHEALTHY else None,
        details=dict(details),
    )


# =============================================================================
# Settings Contract Tests (391)
# =============================================================================


class TestMetaWatchdogSettings391Contract:
    """Design contract values for 391 settings fields."""

    def test_recovery_total_timeout_seconds_default(self):
        """Default: 60.0 (2× probe_interval)."""
        s = MetaWatchdogSettings()
        assert s.recovery_total_timeout_seconds == 60.0

    def test_max_items_per_recovery_default(self):
        """Default: 5."""
        s = MetaWatchdogSettings()
        assert s.max_items_per_recovery == 5

    def test_k8s_api_timeout_seconds_default(self):
        """Default: 30.0."""
        s = MetaWatchdogSettings()
        assert s.k8s_api_timeout_seconds == 30.0

    def test_recovery_total_timeout_seconds_minimum_boundary(self):
        """ge=10.0: value 10.0 passes, 9.9 fails."""
        s = MetaWatchdogSettings(recovery_total_timeout_seconds=10.0)
        assert s.recovery_total_timeout_seconds == 10.0

        with pytest.raises(Exception):
            MetaWatchdogSettings(recovery_total_timeout_seconds=9.9)

    def test_max_items_per_recovery_boundary(self):
        """ge=1, le=50: boundaries pass, outside fails."""
        assert (
            MetaWatchdogSettings(max_items_per_recovery=1).max_items_per_recovery == 1
        )
        assert (
            MetaWatchdogSettings(max_items_per_recovery=50).max_items_per_recovery == 50
        )

        with pytest.raises(Exception):
            MetaWatchdogSettings(max_items_per_recovery=0)
        with pytest.raises(Exception):
            MetaWatchdogSettings(max_items_per_recovery=51)

    def test_k8s_api_timeout_seconds_minimum_boundary(self):
        """ge=5.0: value 5.0 passes, 4.9 fails."""
        s = MetaWatchdogSettings(k8s_api_timeout_seconds=5.0)
        assert s.k8s_api_timeout_seconds == 5.0

        with pytest.raises(Exception):
            MetaWatchdogSettings(k8s_api_timeout_seconds=4.9)

    def test_warn_timeout_vs_probe_warns_when_over_2x(self):
        """UserWarning when recovery_total_timeout > 2× probe_interval."""
        with pytest.warns(UserWarning, match="Watchdog loop may fall behind"):
            MetaWatchdogSettings(
                recovery_total_timeout_seconds=70.0,
                probe_interval_seconds=30.0,
            )

    def test_warn_timeout_vs_probe_no_warning_when_within_2x(self):
        """No warning when recovery_total_timeout <= 2× probe_interval."""
        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter("error", UserWarning)
            MetaWatchdogSettings(
                recovery_total_timeout_seconds=60.0,
                probe_interval_seconds=30.0,
            )


# =============================================================================
# Component Priority Contract Tests (391)
# =============================================================================


# =============================================================================
# Two-Phase check_health Behavior Tests (391)
# =============================================================================


# =============================================================================
# _attempt_guarded_recovery Behavior Tests (391)
# =============================================================================


# =============================================================================
# _build_recovery_fn Behavior Tests (391)
# =============================================================================


# =============================================================================
# _execute_recovery_with_timeout Behavior Tests (391)
# =============================================================================


# =============================================================================
# CB Recovery Impl Behavior Tests (391)
# =============================================================================


# =============================================================================
# Chaos Scheduler Recovery Impl Behavior Tests (391)
# =============================================================================


# =============================================================================
# K8s Effective Timeout Behavior Tests — moved to
# tests/dormant/unit/meta/test_k8s_effective_timeout.py per impl doc 528 D15.
# =============================================================================


# =============================================================================
# InMemory get_open_states Behavior Tests (391)
# =============================================================================


class TestInMemoryGetOpenStatesBehavior:
    """InMemoryCircuitBreakerStateRepository.get_open_states."""

    def test_filters_only_open_states(self):
        """Returns only OPEN states, excluding CLOSED and HALF_OPEN."""
        from baldur.adapters.memory.circuit_breaker import (
            InMemoryCircuitBreakerStateRepository,
        )

        repo = InMemoryCircuitBreakerStateRepository()
        repo.get_or_create("svc-open")
        repo.update_state("svc-open", "open")
        repo.get_or_create("svc-closed")
        repo.update_state("svc-closed", "closed")
        repo.get_or_create("svc-half")
        repo.update_state("svc-half", "half_open")

        result = repo.get_open_states()

        assert len(result) == 1
        assert result[0].service_name == "svc-open"

    def test_limit_applied(self):
        """limit parameter caps results."""
        from baldur.adapters.memory.circuit_breaker import (
            InMemoryCircuitBreakerStateRepository,
        )

        repo = InMemoryCircuitBreakerStateRepository()
        for i in range(10):
            repo.get_or_create(f"svc-{i}")
            repo.update_state(f"svc-{i}", "open")

        result = repo.get_open_states(limit=3)

        assert len(result) == 3

    def test_no_limit_returns_all(self):
        """limit=None returns all OPEN states."""
        from baldur.adapters.memory.circuit_breaker import (
            InMemoryCircuitBreakerStateRepository,
        )

        repo = InMemoryCircuitBreakerStateRepository()
        for i in range(5):
            repo.get_or_create(f"svc-{i}")
            repo.update_state(f"svc-{i}", "open")

        result = repo.get_open_states(limit=None)

        assert len(result) == 5

    def test_empty_repo_returns_empty(self):
        """Empty repository returns empty list."""
        from baldur.adapters.memory.circuit_breaker import (
            InMemoryCircuitBreakerStateRepository,
        )

        repo = InMemoryCircuitBreakerStateRepository()

        result = repo.get_open_states()

        assert result == []
