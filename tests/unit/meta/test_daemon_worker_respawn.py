"""DaemonWorkerProbe respawn coordinator unit tests (impl 489 D7).

Test targets:
    - DaemonWorkerProbe._handle_dead_worker (respawn coordinator)
    - Two-layer counter: handle.restart_count (resettable) vs lifetime
      Prometheus Counter (monotonic)
    - Sustained-health reset gate
    - Backoff elapsed-time gate (non-blocking)
    - DAEMON_WORKER_DIED + DAEMON_WORKER_RESPAWNED event payloads

UNIT_TEST_GUIDELINES.md compliance:
- Behavior verification across the gate axis: respawnable × global_enabled ×
  max_attempts × backoff_gate × sustained_health_window.
- side_effect verification on EventBus emit, restart_count increment, and
  the Prometheus lifetime counter.
"""

from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

import pytest

from baldur.meta.daemon_worker import DaemonWorkerHandle
from baldur.meta.health_probe import DaemonWorkerProbe, HealthStatus


@pytest.fixture(autouse=True)
def _reset_handle_registry():
    from baldur.metrics.recorders import daemon_worker as mod

    with mod._registry_lock:
        snapshot = dict(mod._handle_registry)
        mod._handle_registry.clear()
    yield
    with mod._registry_lock:
        mod._handle_registry.clear()
        mod._handle_registry.update(snapshot)


@pytest.fixture
def enable_respawn(monkeypatch):
    """Flip global ``respawn_enabled`` on for the test."""
    from baldur.settings.daemon_worker import (
        get_daemon_worker_settings,
        reset_daemon_worker_settings,
    )

    reset_daemon_worker_settings()
    settings = get_daemon_worker_settings()
    monkeypatch.setattr(settings, "respawn_enabled", True)
    monkeypatch.setattr(settings, "respawn_max_attempts", 3)
    # Zero backoff so successive ticks in one test do not gate-skip.
    monkeypatch.setattr(settings, "respawn_backoff_base_seconds", 0.0)
    monkeypatch.setattr(settings, "respawn_backoff_max_seconds", 0.1)
    yield settings
    reset_daemon_worker_settings()


def _dead_thread() -> threading.Thread:
    return threading.Thread(target=lambda: None, daemon=True)


def _make_handle(callback=None, **kwargs):
    handle = DaemonWorkerHandle(
        thread=_dead_thread(),
        tick_interval_seconds=1.0,
        restart_callback=callback,
        **kwargs,
    )
    return handle


# =============================================================================
# Behavior — respawn axis
# =============================================================================


class TestDaemonWorkerRespawnBehavior:
    """impl 489 D7: respawn coordinator decision matrix."""

    def test_global_disabled_skips_respawn(self):
        """``respawn_enabled=False`` → callback never fires even if eligible."""
        # Default settings: respawn_enabled=False
        from baldur.metrics.recorders.daemon_worker import register_daemon_worker

        callback = MagicMock()
        handle = _make_handle(callback=callback)
        register_daemon_worker("g-off", handle)

        DaemonWorkerProbe().probe()

        callback.assert_not_called()
        assert handle.restart_count == 0

    def test_no_callback_skips_respawn(self, enable_respawn):
        """Cluster-stateful workers (callback=None) → never respawned."""
        from baldur.metrics.recorders.daemon_worker import register_daemon_worker

        handle = _make_handle(callback=None)
        register_daemon_worker("no-callback", handle)

        DaemonWorkerProbe().probe()

        assert handle.restart_count == 0

    def test_dead_with_callback_fires_callback_and_increments(self, enable_respawn):
        """Eligible respawn → callback invoked + ``restart_count += 1``."""
        from baldur.metrics.recorders.daemon_worker import register_daemon_worker

        callback = MagicMock()
        handle = _make_handle(callback=callback)
        register_daemon_worker("respawn-fires", handle)

        DaemonWorkerProbe().probe()

        callback.assert_called_once()
        assert handle.restart_count == 1
        assert handle.last_respawn_attempt_at is not None

    def test_max_attempts_cap_blocks_further_respawns(self, enable_respawn):
        """At ``restart_count == max_attempts`` the callback no longer fires."""
        from baldur.metrics.recorders.daemon_worker import register_daemon_worker

        callback = MagicMock()
        # Pre-set restart_count to the cap
        handle = _make_handle(callback=callback)
        handle.restart_count = enable_respawn.respawn_max_attempts
        register_daemon_worker("cap-reached", handle)

        DaemonWorkerProbe().probe()

        callback.assert_not_called()
        assert handle.restart_count == enable_respawn.respawn_max_attempts

    def test_backoff_gate_blocks_immediate_retry(self, enable_respawn, monkeypatch):
        """A second probe tick within the backoff window does not respawn again."""
        from baldur.metrics.recorders.daemon_worker import register_daemon_worker

        # Force a non-zero backoff so the elapsed-time gate has effect.
        monkeypatch.setattr(enable_respawn, "respawn_backoff_base_seconds", 5.0)
        monkeypatch.setattr(enable_respawn, "respawn_backoff_max_seconds", 60.0)

        callback = MagicMock()
        handle = _make_handle(callback=callback)
        register_daemon_worker("backoff-gated", handle)

        # First tick: respawn fires.
        DaemonWorkerProbe().probe()
        assert callback.call_count == 1

        # Second tick a moment later: backoff gate blocks the second respawn.
        DaemonWorkerProbe().probe()
        assert callback.call_count == 1

    def test_sustained_health_reset_gate_resets_counter(self, enable_respawn):
        """``last_healthy_observed_at`` older than reset window → ``restart_count = 0``."""
        import time

        from baldur.metrics.recorders.daemon_worker import register_daemon_worker

        callback = MagicMock()
        handle = _make_handle(callback=callback)
        # Pre-set restart_count to the cap so respawn would otherwise be denied.
        handle.restart_count = enable_respawn.respawn_max_attempts
        # Mark "observed healthy" relative to the current monotonic clock so
        # the gate fires regardless of the host's monotonic epoch (Windows
        # ``time.monotonic()`` is system uptime; a freshly-booted host may
        # report a value smaller than ``respawn_count_reset_seconds=3600``).
        handle.last_healthy_observed_at = (
            time.monotonic() - enable_respawn.respawn_count_reset_seconds - 1.0
        )
        register_daemon_worker("sustained-reset", handle)

        DaemonWorkerProbe().probe()

        # Before the cap check, the gate reset restart_count to 0; the
        # respawn proceeded and incremented to 1.
        callback.assert_called_once()
        assert handle.restart_count == 1

    def test_callback_exception_does_not_increment_counter(self, enable_respawn):
        """If ``restart_callback`` raises, ``restart_count`` does NOT increment."""
        from baldur.metrics.recorders.daemon_worker import register_daemon_worker

        callback = MagicMock(side_effect=RuntimeError("spawn failed"))
        handle = _make_handle(callback=callback)
        register_daemon_worker("respawn-raises", handle)

        # Probe must not raise — the coordinator swallows callback errors.
        DaemonWorkerProbe().probe()

        callback.assert_called_once()
        assert handle.restart_count == 0


# =============================================================================
# Behavior — lifetime counter monotonicity
# =============================================================================


class TestDaemonWorkerRestartCounterBehavior:
    """impl 489 D7: lifetime Prometheus Counter is monotonic across resets."""

    def test_lifetime_counter_increments_on_every_respawn(self, enable_respawn):
        """``record_daemon_worker_restart`` is called once per successful respawn."""
        from baldur.metrics.recorders.daemon_worker import register_daemon_worker

        callback = MagicMock()
        handle = _make_handle(callback=callback)
        register_daemon_worker("lifetime-counter", handle)

        with patch(
            "baldur.metrics.recorders.daemon_worker.record_daemon_worker_restart"
        ) as record_mock:
            DaemonWorkerProbe().probe()

        record_mock.assert_called_once_with("lifetime-counter")


# =============================================================================
# Behavior — DAEMON_WORKER_DIED / RESPAWNED event payloads
# =============================================================================


class TestDaemonWorkerDiedEventPayloadBehavior:
    """impl 489 D12: DIED + RESPAWNED EventBus payloads."""

    def test_died_event_emits_with_required_fields(self):
        """DIED event carries worker_name, was_respawnable, age, crash_reason."""
        from baldur.metrics.recorders.daemon_worker import register_daemon_worker

        handle = _make_handle(callback=lambda: None)
        handle.last_crash_reason = "ValueError: bad"
        register_daemon_worker("died-payload", handle)

        bus_mock = MagicMock()
        with patch(
            "baldur.services.event_bus.bus.convenience.get_event_bus",
            return_value=bus_mock,
        ):
            DaemonWorkerProbe().probe()

        # First emit call is DIED.
        died_calls = [
            c
            for c in bus_mock.emit.call_args_list
            if c.args[0].name == "DAEMON_WORKER_DIED"
        ]
        assert len(died_calls) == 1
        data = died_calls[0].kwargs["data"]
        assert data["worker_name"] == "died-payload"
        assert data["was_respawnable"] is True
        assert "last_heartbeat_age_seconds" in data
        assert data["crash_reason"] == "ValueError: bad"

    def test_died_event_was_respawnable_false_for_no_callback(self):
        """``restart_callback=None`` → ``was_respawnable=False`` in payload."""
        from baldur.metrics.recorders.daemon_worker import register_daemon_worker

        handle = _make_handle(callback=None)
        register_daemon_worker("non-respawnable", handle)

        bus_mock = MagicMock()
        with patch(
            "baldur.services.event_bus.bus.convenience.get_event_bus",
            return_value=bus_mock,
        ):
            DaemonWorkerProbe().probe()

        died_calls = [
            c
            for c in bus_mock.emit.call_args_list
            if c.args[0].name == "DAEMON_WORKER_DIED"
        ]
        assert died_calls[0].kwargs["data"]["was_respawnable"] is False

    def test_respawned_event_emits_with_restart_count(self, enable_respawn):
        """RESPAWNED event carries ``worker_name`` and the new ``restart_count``."""
        from baldur.metrics.recorders.daemon_worker import register_daemon_worker

        handle = _make_handle(callback=lambda: None)
        register_daemon_worker("respawned-payload", handle)

        bus_mock = MagicMock()
        with patch(
            "baldur.services.event_bus.bus.convenience.get_event_bus",
            return_value=bus_mock,
        ):
            DaemonWorkerProbe().probe()

        respawned_calls = [
            c
            for c in bus_mock.emit.call_args_list
            if c.args[0].name == "DAEMON_WORKER_RESPAWNED"
        ]
        assert len(respawned_calls) == 1
        data = respawned_calls[0].kwargs["data"]
        assert data["worker_name"] == "respawned-payload"
        assert data["restart_count"] == 1

    def test_died_event_emits_only_once_per_dead_window(self):
        """Repeat probe ticks against the same dead thread emit DIED once."""
        from baldur.metrics.recorders.daemon_worker import register_daemon_worker

        handle = _make_handle(callback=None)
        register_daemon_worker("dedup-died", handle)

        bus_mock = MagicMock()
        with patch(
            "baldur.services.event_bus.bus.convenience.get_event_bus",
            return_value=bus_mock,
        ):
            DaemonWorkerProbe().probe()
            DaemonWorkerProbe().probe()
            DaemonWorkerProbe().probe()

        died_calls = [
            c
            for c in bus_mock.emit.call_args_list
            if c.args[0].name == "DAEMON_WORKER_DIED"
        ]
        assert len(died_calls) == 1


# =============================================================================
# Behavior — overall probe status remains UNHEALTHY during respawn
# =============================================================================


class TestRespawnDoesNotMaskUnhealthyStatusBehavior:
    """A successful respawn this tick still reports UNHEALTHY for this tick.

    The thread observation happened before the callback fired, so the
    probe's status reflects the moment of detection. The newly-spawned
    thread's HEALTHY transition is observable on the next tick.
    """

    def test_respawn_tick_still_reports_unhealthy(self, enable_respawn):
        from baldur.metrics.recorders.daemon_worker import register_daemon_worker

        handle = _make_handle(callback=lambda: None)
        register_daemon_worker("respawn-tick-status", handle)

        result = DaemonWorkerProbe().probe()

        assert result.status == HealthStatus.UNHEALTHY
        assert result.details["workers"]["respawn-tick-status"]["status"] == "DEAD"
