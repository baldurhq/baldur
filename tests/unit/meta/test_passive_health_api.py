"""
Passive health API unit tests for Worker and Gate (411).

Covers:
- Contract: get_passive_health() return dict key/type verification
- Behavior: lifecycle tracking (_last_refresh_at, _started_at, _last_effective_status)
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import patch

import pytest

# =========================================================================
# PrecomputedCacheWorker.get_passive_health()
# =========================================================================


class TestWorkerPassiveHealthContract:
    """Contract verification for Worker.get_passive_health() return structure."""

    def test_passive_health_keys(self):
        """get_passive_health() must return exactly the documented keys."""
        from baldur.services.precomputed_cache.worker import (
            PrecomputedCacheWorker,
        )

        worker = PrecomputedCacheWorker()
        health = worker.get_passive_health()

        expected_keys = {
            "running",
            "registered_keys",
            "last_refresh_at",
            "started_at",
            "refresh_interval_seconds",
            "effective_interval_seconds",
        }
        assert set(health.keys()) == expected_keys

    def test_initial_state_values(self):
        """Fresh worker: running=False, no timestamps, empty keys."""
        from baldur.services.precomputed_cache.worker import (
            PrecomputedCacheWorker,
        )

        worker = PrecomputedCacheWorker()
        health = worker.get_passive_health()

        assert health["running"] is False
        assert health["registered_keys"] == []
        assert health["last_refresh_at"] is None
        assert health["started_at"] is None
        assert isinstance(health["refresh_interval_seconds"], (int, float))


class TestWorkerPassiveHealthBehavior:
    """Behavior verification for Worker passive health lifecycle."""

    def test_start_sets_started_at(self):
        """start() must set _started_at timestamp."""
        from baldur.services.precomputed_cache.worker import (
            PrecomputedCacheWorker,
        )

        worker = PrecomputedCacheWorker()
        assert worker._started_at is None

        # Patch _schedule_refresh to avoid real Timer
        with patch.object(worker, "_schedule_refresh"):
            worker.start()

        assert worker._started_at is not None
        assert isinstance(worker._started_at, datetime)

    def test_start_idempotent_preserves_started_at(self):
        """Double start() must not change _started_at."""
        from baldur.services.precomputed_cache.worker import (
            PrecomputedCacheWorker,
        )

        worker = PrecomputedCacheWorker()
        with patch.object(worker, "_schedule_refresh"):
            worker.start()
            first_started = worker._started_at
            worker.start()

        assert worker._started_at is first_started

    def test_registered_keys_reflected_in_passive_health(self):
        """Registered compute functions appear in passive health."""
        from baldur.services.precomputed_cache.worker import (
            PrecomputedCacheWorker,
        )

        worker = PrecomputedCacheWorker()
        worker.register("stats_cache", lambda: {"value": 1})
        worker.register("dashboard_cache", lambda: {"value": 2})

        health = worker.get_passive_health()
        assert sorted(health["registered_keys"]) == [
            "dashboard_cache",
            "stats_cache",
        ]

    def test_passive_health_returns_iso_timestamps(self):
        """Timestamps must be ISO format strings when set."""
        from baldur.services.precomputed_cache.worker import (
            PrecomputedCacheWorker,
        )

        worker = PrecomputedCacheWorker()
        with patch.object(worker, "_schedule_refresh"):
            worker.start()

        health = worker.get_passive_health()
        started_at = health["started_at"]
        assert started_at is not None
        # Must be parseable as ISO format
        parsed = datetime.fromisoformat(started_at)
        assert isinstance(parsed, datetime)


# =========================================================================
# ErrorBudgetGate.get_passive_health()
# =========================================================================


@pytest.fixture
def gate_instance(monkeypatch):
    """Create a Gate instance with mocked dependencies.

    Per impl 527 (v1.1 deferred), BALDUR_ERROR_BUDGET_GATE_ENABLED defaults
    to False. These tests expect an enabled gate, so re-enable via env var.
    """
    pytest.importorskip("baldur_pro")
    from baldur.settings.error_budget_gate import (
        reset_error_budget_gate_settings,
    )

    monkeypatch.setenv("BALDUR_ERROR_BUDGET_GATE_ENABLED", "true")
    reset_error_budget_gate_settings()

    with patch(
        "baldur_pro.services.error_budget_gate.gate.ErrorBudgetGate._load_config"
    ):
        # Pass a config instance constructed AFTER env var is set, so enabled=True
        from baldur_pro.services.error_budget_gate.config import (
            ErrorBudgetGateConfig,
        )
        from baldur_pro.services.error_budget_gate.gate import ErrorBudgetGate

        gate = ErrorBudgetGate(config=ErrorBudgetGateConfig())
    try:
        yield gate
    finally:
        reset_error_budget_gate_settings()


class TestGatePassiveHealthContract:
    """Contract verification for Gate.get_passive_health() return structure."""

    def test_passive_health_keys(self, gate_instance):
        """get_passive_health() must return exactly the documented keys."""
        health = gate_instance.get_passive_health()

        expected_keys = {
            "enabled",
            "effective_status",
            "current_status",
            "fault_detector_state",
            "fault_detector_failures",
            "fail_open_triggered",
            "last_checked_at",
        }
        assert set(health.keys()) == expected_keys

    def test_initial_state_values(self, gate_instance):
        """Fresh gate: enabled=True, OPEN status, no checked_at."""
        health = gate_instance.get_passive_health()

        assert health["enabled"] is True
        assert health["effective_status"] == "open"
        assert health["current_status"] == "open"
        assert health["fail_open_triggered"] is False
        assert health["last_checked_at"] is None


class TestGatePassiveHealthBehavior:
    """Behavior verification for Gate passive health tracking."""

    def test_disabled_check_updates_effective_status(self, gate_instance):
        """check() on disabled gate sets _last_effective_status to DISABLED."""
        from baldur_pro.services.error_budget_gate.config import GateStatus

        gate_instance._config.enabled = False
        gate_instance.check()

        assert gate_instance._last_effective_status == GateStatus.DISABLED
        assert gate_instance._last_checked_at is not None

        health = gate_instance.get_passive_health()
        assert health["effective_status"] == "disabled"
        assert health["last_checked_at"] is not None

    def test_normal_check_updates_tracking_fields(self, gate_instance):
        """Normal check() updates both _last_effective_status and _last_checked_at."""
        # Mock _get_error_budget_percent to return a good budget
        with patch.object(
            gate_instance, "_get_error_budget_percent", return_value=90.0
        ):
            gate_instance.check()

        assert gate_instance._last_checked_at is not None
        health = gate_instance.get_passive_health()
        assert health["last_checked_at"] is not None
        parsed = datetime.fromisoformat(health["last_checked_at"])
        assert isinstance(parsed, datetime)

    def test_fail_open_check_updates_effective_status(self, gate_instance):
        """Fail-open check() sets effective_status to fail_open."""

        # budget_percent=None triggers fail-open
        with patch.object(
            gate_instance, "_get_error_budget_percent", return_value=None
        ):
            gate_instance.check()

        health = gate_instance.get_passive_health()
        assert health["effective_status"] in ("fail_open", "fail_open_rate_limited")
        assert health["fail_open_triggered"] is True

    def test_fail_open_triggered_false_for_normal_status(self, gate_instance):
        """fail_open_triggered is False for OPEN/WARNING/BLOCKED."""
        from baldur_pro.services.error_budget_gate.config import GateStatus

        for status in (GateStatus.OPEN, GateStatus.WARNING, GateStatus.BLOCKED):
            gate_instance._last_effective_status = status
            health = gate_instance.get_passive_health()
            assert health["fail_open_triggered"] is False

    def test_fail_open_triggered_true_for_fail_statuses(self, gate_instance):
        """fail_open_triggered is True for FAIL_OPEN and FAIL_OPEN_RATE_LIMITED."""
        from baldur_pro.services.error_budget_gate.config import GateStatus

        for status in (GateStatus.FAIL_OPEN, GateStatus.FAIL_OPEN_RATE_LIMITED):
            gate_instance._last_effective_status = status
            health = gate_instance.get_passive_health()
            assert health["fail_open_triggered"] is True
