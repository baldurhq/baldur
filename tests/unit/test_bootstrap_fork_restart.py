"""609 — framework-agnostic post-fork background-worker restart.

Covers the net-new 609 surface: the single ``start_background_workers()`` entry
point and its ``_BACKGROUND_WORKER_STARTERS`` registry (D4), plus the end-to-end
fork-restart matrix that the registry + per-starter ``is_gunicorn_master()`` skip
implement together (D1/D3/D7).

- ``_BACKGROUND_WORKER_STARTERS`` membership/order is the drift guard against the
  two start-lists silently diverging (G4) — a Contract assertion.
- ``start_background_workers()`` iterates every registered starter (delegation)
  and never propagates a starter's failure (fail-soft, because each starter
  swallows its own ImportError/Exception — the loop has no try/except of its own).
- Fork matrix, driven purely by ``monkeypatch.setenv`` (``is_gunicorn_master()``
  keys on ``SERVER_SOFTWARE`` / ``GUNICORN_WORKER`` only — no real ``fork()``):
  under the Gunicorn master nothing starts; after ``post_worker_init`` sets
  ``GUNICORN_WORKER=1`` every enabled worker starts; under non-gunicorn
  (runserver / CLI) ``init()`` starts them directly; a second call is idempotent.

The two cleanly-observable OSS workers — ``precomputed_cache`` and
``system_metrics_cache`` (both default-ON, both exposing ``is_running()``) — carry
the real-state matrix assertions. ``meta_watchdog`` is a no-op in OSS
(``selfhealer_watchdog.safe_get()`` returns None without ``baldur_pro``) and the
two Group-B workers default ``enabled=False``, so they do not participate in the
real-start matrix; their per-starter master-skip is covered alongside their
existing gating tests in ``test_bootstrap_background_services.py`` /
``test_bootstrap_system_metrics_cache.py``.

Real-worker tests ``reset_*`` every started worker on setup/teardown so the suite
leaks no daemon ``threading.Timer`` (the failure the full-suite SC guards
against).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from baldur import bootstrap


@pytest.fixture(autouse=True)
def _reset_background_workers():
    """Reset the two real OSS workers (and the Group-B singletons) on setup and
    teardown so a real ``start()`` never leaks a daemon timer into the suite and
    no prior test's leaked worker pollutes the "not running" assertions."""
    from baldur.services.capacity_reservation.service import (
        CapacityReservationService,
    )
    from baldur.services.cell_topology.service import reset_cell_topology_service
    from baldur.services.precomputed_cache import reset_precomputed_cache_worker
    from baldur.services.system_metrics_cache import reset_system_metrics_cache

    def _reset_all():
        reset_precomputed_cache_worker()
        reset_system_metrics_cache()
        reset_cell_topology_service()
        CapacityReservationService.reset()

    _reset_all()
    yield
    _reset_all()


@pytest.fixture
def non_gunicorn_env(monkeypatch):
    """Strip the gunicorn env so ``is_gunicorn_master()`` returns False — the
    Django-runserver / plain-Python-CLI process model."""
    monkeypatch.delenv("SERVER_SOFTWARE", raising=False)
    monkeypatch.delenv("GUNICORN_WORKER", raising=False)


@pytest.fixture
def gunicorn_master_env(monkeypatch):
    """Simulate the Gunicorn master / pre-``post_worker_init`` worker:
    ``SERVER_SOFTWARE`` set, ``GUNICORN_WORKER`` unset → ``is_gunicorn_master()``
    returns True."""
    monkeypatch.setenv("SERVER_SOFTWARE", "gunicorn/21.2.0")
    monkeypatch.delenv("GUNICORN_WORKER", raising=False)


@pytest.fixture
def observable_autostart(monkeypatch):
    """Re-enable the autostart hatch for the two observable workers (the unit-test
    process pins both to ``0`` in tests/conftest.py). ``meta_watchdog`` is left at
    ``0`` so the matrix never depends on ``baldur_pro`` being installed."""
    monkeypatch.setenv("BALDUR_PRECOMPUTED_CACHE_AUTOSTART", "1")
    monkeypatch.setenv("BALDUR_SYSTEM_METRICS_CACHE_AUTOSTART", "1")


class TestBackgroundWorkerRegistryContract:
    """D4: the OSS starter registry is the single source of truth — its exact
    membership is the drift guard against the init() chain and the post-fork hook
    diverging (G4). 615 D4 widened it from 5 to 7 with the scaling loops; the
    CB-state startup seed widened it to 8."""

    def test_registry_contains_exactly_the_eight_oss_starters(self):
        """Hardcoded set-equality against the eight expected starter callables."""
        expected = {
            bootstrap._start_capacity_reservation_if_enabled,
            bootstrap._start_cell_topology_if_enabled,
            bootstrap._start_meta_watchdog_if_enabled,
            bootstrap._start_precomputed_cache_if_enabled,
            bootstrap._start_system_metrics_cache_if_enabled,
            bootstrap._start_rate_controller_if_enabled,
            bootstrap._start_hpa_exporter_if_enabled,
            bootstrap._seed_circuit_breaker_state_if_enabled,
        }

        assert set(bootstrap._BACKGROUND_WORKER_STARTERS) == expected
        # No duplicate entries — set size collapses to the tuple length only when
        # every starter is distinct.
        assert len(bootstrap._BACKGROUND_WORKER_STARTERS) == len(expected) == 8

    def test_registry_entries_are_all_callable(self):
        assert all(callable(s) for s in bootstrap._BACKGROUND_WORKER_STARTERS)


class TestStartBackgroundWorkersBehavior:
    """D4: ``start_background_workers()`` iterates the registry — delegating to
    every starter and never propagating an individual starter's failure."""

    def test_invokes_every_registered_starter_once(self):
        # Given a registry of five spy starters.
        spies = [MagicMock(name=f"starter_{i}") for i in range(5)]

        # When start_background_workers() iterates it.
        with patch.object(bootstrap, "_BACKGROUND_WORKER_STARTERS", tuple(spies)):
            bootstrap.start_background_workers()

        # Then each starter is invoked exactly once, with no arguments.
        for spy in spies:
            spy.assert_called_once_with()

    def test_failing_starter_does_not_abort_later_starters(
        self, non_gunicorn_env, monkeypatch
    ):
        """A starter whose body raises must not abort the rest.

        ``start_background_workers()`` has no try/except of its own — each starter
        is independently fail-soft (it swallows its own ImportError/Exception), so
        the loop can never observe an exception. Force the 4th starter
        (precomputed_cache) to raise inside its body and assert the 5th
        (system_metrics_cache) still runs and the call does not propagate.
        """
        monkeypatch.setenv("BALDUR_PRECOMPUTED_CACHE_AUTOSTART", "1")
        monkeypatch.setenv("BALDUR_SYSTEM_METRICS_CACHE_AUTOSTART", "1")

        with (
            patch(
                "baldur.settings.precomputed_cache.get_precomputed_cache_settings",
                side_effect=RuntimeError("boom inside the 4th starter"),
            ),
            patch(
                "baldur.services.system_metrics_cache.start_system_metrics_cache"
            ) as smc_start,
        ):
            bootstrap.start_background_workers()  # must not raise

        smc_start.assert_called_once()

    def test_empty_startup_integrations_slot_iterates_to_noop(self):
        """615 D1: an empty ``startup_integrations`` slot (OSS-only / unentitled
        install — the state of this test process) iterates to a no-op.

        The OSS tuple is patched empty so the assertion isolates the slot
        iteration; an empty slot means ``start_background_workers()`` completes
        without touching any PRO starter and without raising.
        """
        from baldur.factory.registry import ProviderRegistry

        assert ProviderRegistry.startup_integrations.list_providers() == []

        with patch.object(bootstrap, "_BACKGROUND_WORKER_STARTERS", ()):
            bootstrap.start_background_workers()  # must not raise

        # No starter was registered or invoked-and-cached by the iteration.
        assert ProviderRegistry.startup_integrations.list_providers() == []
        assert ProviderRegistry.startup_integrations.instance_count() == 0

    def test_populated_slot_invokes_every_starter_once(self):
        """615 D1: each ``startup_integrations`` provider is invoked exactly once
        per ``start_background_workers()`` call, after the OSS tuple."""
        from baldur.factory.registry import ProviderRegistry

        # Given two spy starters registered into the slot.
        spy_a = MagicMock(name="slot_starter_a")
        spy_b = MagicMock(name="slot_starter_b")
        ProviderRegistry.startup_integrations.register("slot_a", spy_a)
        ProviderRegistry.startup_integrations.register("slot_b", spy_b)

        # When the OSS tuple is empty so only the slot iteration runs.
        with patch.object(bootstrap, "_BACKGROUND_WORKER_STARTERS", ()):
            bootstrap.start_background_workers()

        # Then each slot starter is invoked exactly once, with no arguments.
        spy_a.assert_called_once_with()
        spy_b.assert_called_once_with()

    def test_failing_slot_starter_does_not_abort_later_slot_starters(self):
        """615 D1: the per-name try/except around the slot iteration mirrors the
        shutdown-integration consumer shape — a raising starter cannot abort the
        remaining ones, and the call never propagates."""
        from baldur.factory.registry import ProviderRegistry

        # Given a raising starter registered before a healthy one.
        boom = MagicMock(name="boom", side_effect=RuntimeError("slot boom"))
        spy_ok = MagicMock(name="slot_ok")
        ProviderRegistry.startup_integrations.register("boom", boom)
        ProviderRegistry.startup_integrations.register("ok", spy_ok)

        # When the slot is iterated (OSS tuple empty).
        with patch.object(bootstrap, "_BACKGROUND_WORKER_STARTERS", ()):
            bootstrap.start_background_workers()  # must not raise

        # Then the healthy starter after the failure still ran.
        boom.assert_called_once_with()
        spy_ok.assert_called_once_with()


class TestForkRestartMatrixBehavior:
    """D1/D3/D7: the registry + per-starter master-skip, exercised end-to-end via
    ``start_background_workers()`` across the env-var fork matrix. Observed on the
    two default-ON OSS workers that expose real ``is_running()`` state."""

    @staticmethod
    def _running_states():
        from baldur.services.precomputed_cache import get_precomputed_cache_worker
        from baldur.services.system_metrics_cache import get_system_metrics_cache

        return (
            get_precomputed_cache_worker().is_running(),
            get_system_metrics_cache().is_running(),
        )

    def test_no_workers_start_under_gunicorn_master(
        self, gunicorn_master_env, observable_autostart
    ):
        """Under the master (``--preload`` load time / pre-hook worker), every
        starter's ``is_gunicorn_master()`` skip fires → nothing starts."""
        bootstrap.start_background_workers()

        assert self._running_states() == (False, False)

    def test_workers_start_in_worker_after_post_worker_init(
        self, gunicorn_master_env, observable_autostart, monkeypatch
    ):
        """SC3: nothing runs under the master; once ``post_worker_init`` sets
        ``GUNICORN_WORKER=1`` the same call starts every enabled worker."""
        # Master / pre-hook: the skip suppresses every start.
        bootstrap.start_background_workers()
        assert self._running_states() == (False, False)

        # post_worker_init sets GUNICORN_WORKER=1 → is_gunicorn_master() → False.
        monkeypatch.setenv("GUNICORN_WORKER", "1")
        bootstrap.start_background_workers()

        assert self._running_states() == (True, True)

    def test_workers_start_under_non_gunicorn_runserver(
        self, non_gunicorn_env, observable_autostart
    ):
        """Non-gunicorn (Django runserver / plain-Python CLI): ``init()`` →
        ``start_background_workers()`` starts every enabled worker directly — the
        master-skip is a no-op when ``SERVER_SOFTWARE`` is unset."""
        bootstrap.start_background_workers()

        assert self._running_states() == (True, True)

    def test_double_start_keeps_exactly_one_live_worker(
        self, non_gunicorn_env, observable_autostart
    ):
        """SC7: a second ``start_background_workers()`` (the init()-then-restart
        double-call) does not spawn a second live worker — the service-level
        ``_running`` guard short-circuits the repeat start.

        ``precomputed_cache`` exposes ``_started_at``, set only inside ``start()``
        *after* the guard and untouched by the refresh timer, so an unchanged
        ``_started_at`` proves the second ``start()`` returned early without
        re-entering the body (a timing-independent idempotency signal).
        """
        from baldur.services.precomputed_cache import get_precomputed_cache_worker
        from baldur.services.system_metrics_cache import get_system_metrics_cache

        bootstrap.start_background_workers()
        pc_worker = get_precomputed_cache_worker()
        smc = get_system_metrics_cache()
        first_started_at = pc_worker._started_at

        bootstrap.start_background_workers()

        # Same singletons, still running.
        assert get_precomputed_cache_worker() is pc_worker
        assert get_system_metrics_cache() is smc
        assert pc_worker.is_running() is True
        assert smc.is_running() is True
        # The _running guard short-circuited the second start() — no re-entry.
        assert pc_worker._started_at == first_started_at
