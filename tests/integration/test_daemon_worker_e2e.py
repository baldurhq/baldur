"""Daemon worker observability + respawn end-to-end integration test (impl 489).

Mock-based — no infrastructure (Redis/K8s/Docker not required). Wires the
full chain: ``DaemonWorkerHandle`` registration → ``DaemonWorkerProbe`` tick
→ DAEMON_WORKER_DIED EventBus event → ``DLQOutboxWorker._worker_dead``
toggle → respawn callback → DAEMON_WORKER_RESPAWNED event → flag clear.

Scenarios from impl 489 D9 / Test Assessment:
    1. Single-worker kill → UNHEALTHY → DIED → respawn → RESPAWNED → counter
    2. Respawn storm — N workers killed in one tick fire N independent restarts
    3. Graceful stop — no spurious UNHEALTHY/respawn during is_stopping window
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
def respawn_enabled(monkeypatch):
    from baldur.settings.daemon_worker import (
        get_daemon_worker_settings,
        reset_daemon_worker_settings,
    )

    reset_daemon_worker_settings()
    settings = get_daemon_worker_settings()
    monkeypatch.setattr(settings, "respawn_enabled", True)
    monkeypatch.setattr(settings, "respawn_max_attempts", 5)
    monkeypatch.setattr(settings, "respawn_backoff_base_seconds", 0.0)
    monkeypatch.setattr(settings, "respawn_backoff_max_seconds", 0.1)
    yield settings
    reset_daemon_worker_settings()


def _dead_thread() -> threading.Thread:
    return threading.Thread(target=lambda: None, daemon=True)


# =============================================================================
# Scenario 1 — kill → DIED → respawn → RESPAWNED → flag toggle
# =============================================================================


class TestDaemonWorkerKillRespawnE2E:
    """Scenario 1: full DIED→respawn→RESPAWNED round-trip on a single worker."""

    def test_kill_emits_died_then_respawn_emits_respawned(self, respawn_enabled):
        from baldur.metrics.recorders.daemon_worker import register_daemon_worker

        spawn_calls: list[threading.Thread] = []

        def restart_callback() -> None:
            new_thread = _dead_thread()
            spawn_calls.append(new_thread)
            handle.thread = new_thread

        handle = DaemonWorkerHandle(
            thread=_dead_thread(),
            tick_interval_seconds=1.0,
            restart_callback=restart_callback,
        )
        register_daemon_worker("e2e-single", handle)

        bus_mock = MagicMock()
        with (
            patch(
                "baldur.services.event_bus.bus.convenience.get_event_bus",
                return_value=bus_mock,
            ),
            patch(
                "baldur.metrics.recorders.daemon_worker.record_daemon_worker_restart"
            ) as record_mock,
        ):
            result = DaemonWorkerProbe().probe()

        # Probe reports UNHEALTHY for this tick (thread observed dead).
        assert result.status == HealthStatus.UNHEALTHY

        # Both lifecycle events emitted in order.
        emitted = [c.args[0].name for c in bus_mock.emit.call_args_list]
        assert emitted == ["DAEMON_WORKER_DIED", "DAEMON_WORKER_RESPAWNED"]

        # Respawn callback fired exactly once.
        assert len(spawn_calls) == 1
        # Two-layer counter: gate counter +1 AND lifetime counter recorded.
        assert handle.restart_count == 1
        record_mock.assert_called_once_with("e2e-single")

    def test_dlq_outbox_worker_dead_flag_toggles_on_event_round_trip(
        self, respawn_enabled
    ):
        """impl 489 D8: ``_worker_dead`` flips True on DIED, False on RESPAWNED.

        Wires the ``outbox`` module's two subscribers directly to a
        synchronous event bus stand-in so the DIED→True→RESPAWNED→False
        round-trip is observable without spinning up a real EventBus.
        """
        from baldur.services.dlq_outbox import outbox as outbox_module

        # Verify starting state.
        outbox_module._worker_dead = False
        try:
            died_event = MagicMock()
            died_event.data = {"worker_name": "DLQOutboxWorker"}
            respawned_event = MagicMock()
            respawned_event.data = {"worker_name": "DLQOutboxWorker"}

            outbox_module._on_daemon_worker_died(died_event)
            assert outbox_module.is_worker_dead() is True

            outbox_module._on_daemon_worker_respawned(respawned_event)
            assert outbox_module.is_worker_dead() is False
        finally:
            outbox_module._worker_dead = False

    def test_dlq_outbox_filter_ignores_other_worker_names(self, respawn_enabled):
        """A different worker's DIED event must NOT toggle dlq_outbox's flag."""
        from baldur.services.dlq_outbox import outbox as outbox_module

        outbox_module._worker_dead = False
        try:
            other_event = MagicMock()
            other_event.data = {"worker_name": "AuditWatchdog"}

            outbox_module._on_daemon_worker_died(other_event)

            assert outbox_module.is_worker_dead() is False
        finally:
            outbox_module._worker_dead = False


# =============================================================================
# Scenario 2 — respawn storm
# =============================================================================


class TestDaemonWorkerRespawnStormE2E:
    """Scenario 2: N dead workers in one probe tick → N independent respawns."""

    def test_n_dead_workers_each_respawn_independently(self, respawn_enabled):
        from baldur.metrics.recorders.daemon_worker import register_daemon_worker

        N = 5
        spawn_counts = {f"storm-{i}": 0 for i in range(N)}
        handles = {}

        for i in range(N):
            name = f"storm-{i}"

            def make_callback(n: str):
                def cb() -> None:
                    spawn_counts[n] += 1

                return cb

            handle = DaemonWorkerHandle(
                thread=_dead_thread(),
                tick_interval_seconds=1.0,
                restart_callback=make_callback(name),
            )
            handles[name] = handle
            register_daemon_worker(name, handle)

        bus_mock = MagicMock()
        with patch(
            "baldur.services.event_bus.bus.convenience.get_event_bus",
            return_value=bus_mock,
        ):
            DaemonWorkerProbe().probe()

        # Each worker respawned exactly once.
        for n in spawn_counts:
            assert spawn_counts[n] == 1, f"{n} did not respawn"
            assert handles[n].restart_count == 1

        # N DIED + N RESPAWNED events emitted.
        emitted = [c.args[0].name for c in bus_mock.emit.call_args_list]
        assert emitted.count("DAEMON_WORKER_DIED") == N
        assert emitted.count("DAEMON_WORKER_RESPAWNED") == N


# =============================================================================
# Scenario 3 — graceful stop
# =============================================================================


class TestDaemonWorkerGracefulStopE2E:
    """Scenario 3: ``is_stopping=True`` window suppresses spurious UNHEALTHY+respawn."""

    def test_is_stopping_window_no_unhealthy_no_respawn(self, respawn_enabled):
        """During the graceful-stop window, the dead thread is reported STOPPING."""
        from baldur.metrics.recorders.daemon_worker import register_daemon_worker

        callback = MagicMock()
        handle = DaemonWorkerHandle(
            thread=_dead_thread(),
            tick_interval_seconds=1.0,
            restart_callback=callback,
        )
        handle.is_stopping = True
        register_daemon_worker("graceful-stop", handle)

        bus_mock = MagicMock()
        with patch(
            "baldur.services.event_bus.bus.convenience.get_event_bus",
            return_value=bus_mock,
        ):
            result = DaemonWorkerProbe().probe()

        # Status is HEALTHY (no other unhealthy workers).
        assert result.status == HealthStatus.HEALTHY
        assert result.details["workers"]["graceful-stop"]["status"] == "STOPPING"
        # No respawn fired.
        callback.assert_not_called()
        assert handle.restart_count == 0
        # No DIED / RESPAWNED events for the stopping worker.
        for call in bus_mock.emit.call_args_list:
            assert call.args[0].name not in (
                "DAEMON_WORKER_DIED",
                "DAEMON_WORKER_RESPAWNED",
            )

    def test_dlq_outbox_worker_stop_unregisters_handle(self):
        """``DLQOutboxWorker.stop()`` removes the handle from the registry."""
        from baldur.adapters.memory import InMemoryFailedOperationRepository
        from baldur.audit.ring_buffer import RingBuffer
        from baldur.metrics.recorders.daemon_worker import (
            get_registered_daemon_workers,
        )
        from baldur.services.dlq_outbox.worker import DLQOutboxWorker
        from baldur.settings.backpressure import BackpressureStrategy

        # Wire a real worker against an in-memory sink — exercises the
        # actual register/unregister path.
        repo = InMemoryFailedOperationRepository()  # noqa: F841 — captured by closure
        buffer = RingBuffer(capacity=10, strategy=BackpressureStrategy.DROP_OLDEST)
        worker = DLQOutboxWorker(
            buffer=buffer,
            sync_writer=lambda kwargs: None,
            batch_size=1,
            flush_interval_seconds=0.01,
        )
        worker.start()
        try:
            assert "DLQOutboxWorker" in get_registered_daemon_workers()
        finally:
            worker.stop(timeout=1.0)

        # After stop(), the registry slot is gone.
        assert "DLQOutboxWorker" not in get_registered_daemon_workers()
