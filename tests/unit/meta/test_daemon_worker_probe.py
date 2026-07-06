"""DaemonWorkerProbe unit tests (impl 489 D3).

Test targets:
    - baldur.meta.health_probe.DaemonWorkerProbe.probe()
    - Per-worker status routing: HEALTHY / STALE / DEAD / STOPPING
    - last_healthy_observed_at update on HEALTHY observation

Test Categories:
    A. Behavior — per-status routing
    B. Behavior — last_healthy_observed_at gate state
    C. Contract — empty registry yields HEALTHY
"""

from __future__ import annotations

import threading

import pytest

from baldur.meta.daemon_worker import DaemonWorkerHandle
from baldur.meta.health_probe import DaemonWorkerProbe, HealthStatus


@pytest.fixture(autouse=True)
def _reset_handle_registry():
    """Snapshot+clear the handle registry so tests start from a known state."""
    from baldur.metrics.recorders import daemon_worker as mod

    with mod._registry_lock:
        snapshot = dict(mod._handle_registry)
        mod._handle_registry.clear()
    yield
    with mod._registry_lock:
        mod._handle_registry.clear()
        mod._handle_registry.update(snapshot)


@pytest.fixture(autouse=True)
def _disable_respawn(monkeypatch):
    """Default: respawn is OFF for these probe-state tests.

    The respawn coordinator runs inside ``probe()`` on dead detection. To keep
    the probe-state tests focused on status routing, we disable global
    respawn here. Respawn behavior is covered in
    ``tests/unit/meta/test_daemon_worker_respawn.py``.
    """
    from baldur.settings.daemon_worker import (
        get_daemon_worker_settings,
        reset_daemon_worker_settings,
    )

    reset_daemon_worker_settings()
    settings = get_daemon_worker_settings()
    monkeypatch.setattr(settings, "respawn_enabled", False)
    yield
    reset_daemon_worker_settings()


def _live_thread(stop_event: threading.Event) -> threading.Thread:
    t = threading.Thread(target=lambda: stop_event.wait(timeout=5.0), daemon=True)
    t.start()
    return t


def _dead_thread() -> threading.Thread:
    """An unstarted Thread (is_alive() == False)."""
    return threading.Thread(target=lambda: None, daemon=True)


# =============================================================================
# A. Behavior — per-status routing
# =============================================================================


class TestDaemonWorkerProbeBehavior:
    """impl 489 D3: probe maps each handle to HEALTHY/STALE/DEAD/STOPPING."""

    def test_probe_component_name_is_daemon_workers(self):
        assert DaemonWorkerProbe().component_name == "daemon_workers"

    def test_alive_recent_heartbeat_routes_to_healthy(self):
        # Given
        from baldur.metrics.recorders.daemon_worker import register_daemon_worker

        stop = threading.Event()
        try:
            handle = DaemonWorkerHandle(
                thread=_live_thread(stop), tick_interval_seconds=1.0
            )
            register_daemon_worker("alive-fresh", handle)

            # When
            result = DaemonWorkerProbe().probe()

            # Then
            assert result.status == HealthStatus.HEALTHY
            assert result.details["workers"]["alive-fresh"]["status"] == "HEALTHY"
        finally:
            stop.set()

    def test_dead_thread_routes_to_unhealthy(self):
        # Given
        from baldur.metrics.recorders.daemon_worker import register_daemon_worker

        handle = DaemonWorkerHandle(thread=_dead_thread(), tick_interval_seconds=1.0)
        register_daemon_worker("dead-worker", handle)

        # When
        result = DaemonWorkerProbe().probe()

        # Then
        assert result.status == HealthStatus.UNHEALTHY
        assert result.details["workers"]["dead-worker"]["status"] == "DEAD"
        assert "dead-worker" in result.reason

    def test_alive_but_stale_heartbeat_routes_to_unhealthy(self):
        """Heartbeat older than ``staleness_threshold_seconds`` → UNHEALTHY/STALE."""
        # Given
        from baldur.metrics.recorders.daemon_worker import register_daemon_worker

        stop = threading.Event()
        try:
            # last_heartbeat_at far in the past relative to time.monotonic()
            handle = DaemonWorkerHandle(
                thread=_live_thread(stop),
                tick_interval_seconds=1.0,
                staleness_threshold_seconds=2.0,
                last_heartbeat_at=0.0,
            )
            register_daemon_worker("stale-worker", handle)

            # When
            result = DaemonWorkerProbe().probe()

            # Then
            assert result.status == HealthStatus.UNHEALTHY
            assert result.details["workers"]["stale-worker"]["status"] == "STALE"
        finally:
            stop.set()

    def test_is_stopping_skips_status_evaluation(self):
        """``is_stopping=True`` is reported as STOPPING and never UNHEALTHY."""
        # Given — a dead thread that would normally route to UNHEALTHY
        from baldur.metrics.recorders.daemon_worker import register_daemon_worker

        handle = DaemonWorkerHandle(thread=_dead_thread(), tick_interval_seconds=1.0)
        handle.is_stopping = True
        register_daemon_worker("stopping-worker", handle)

        # When
        result = DaemonWorkerProbe().probe()

        # Then — no false UNHEALTHY during the graceful-stop window
        assert result.status == HealthStatus.HEALTHY
        assert result.details["workers"]["stopping-worker"]["status"] == "STOPPING"

    def test_mixed_registry_reports_worst_status(self):
        """One DEAD + one HEALTHY → overall UNHEALTHY, both rows present."""
        # Given
        from baldur.metrics.recorders.daemon_worker import register_daemon_worker

        stop = threading.Event()
        try:
            healthy = DaemonWorkerHandle(
                thread=_live_thread(stop), tick_interval_seconds=1.0
            )
            dead = DaemonWorkerHandle(thread=_dead_thread(), tick_interval_seconds=1.0)
            register_daemon_worker("h", healthy)
            register_daemon_worker("d", dead)

            # When
            result = DaemonWorkerProbe().probe()

            # Then
            assert result.status == HealthStatus.UNHEALTHY
            statuses = {
                name: info["status"] for name, info in result.details["workers"].items()
            }
            assert statuses == {"h": "HEALTHY", "d": "DEAD"}
            assert result.details["total"] == 2
        finally:
            stop.set()


# =============================================================================
# B. Behavior — last_healthy_observed_at update on HEALTHY observation
# =============================================================================


class TestDaemonWorkerProbeHealthMarkingBehavior:
    """impl 489 D3: HEALTHY tick stamps ``handle.last_healthy_observed_at``."""

    def test_healthy_observation_stamps_last_healthy_observed_at(self):
        from baldur.metrics.recorders.daemon_worker import register_daemon_worker

        stop = threading.Event()
        try:
            handle = DaemonWorkerHandle(
                thread=_live_thread(stop), tick_interval_seconds=1.0
            )
            register_daemon_worker("mark-healthy", handle)
            assert handle.last_healthy_observed_at is None

            DaemonWorkerProbe().probe()

            assert handle.last_healthy_observed_at is not None
            assert handle.last_healthy_observed_at > 0
        finally:
            stop.set()

    def test_unhealthy_observation_does_not_stamp(self):
        from baldur.metrics.recorders.daemon_worker import register_daemon_worker

        handle = DaemonWorkerHandle(thread=_dead_thread(), tick_interval_seconds=1.0)
        register_daemon_worker("no-mark", handle)

        DaemonWorkerProbe().probe()

        # Dead worker: last_healthy_observed_at remains None.
        assert handle.last_healthy_observed_at is None


# =============================================================================
# C. Contract — empty registry
# =============================================================================


class TestDaemonWorkerProbeEmptyContract:
    """impl 489 D3: empty registry → HEALTHY with zero rows."""

    def test_empty_registry_yields_healthy(self):
        result = DaemonWorkerProbe().probe()

        assert result.status == HealthStatus.HEALTHY
        assert result.details["total"] == 0
        assert result.details["workers"] == {}
        assert result.reason == ""
