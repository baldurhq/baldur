"""
317: BaldurConfig orphan service wiring unit tests.

Test targets:
- _initialize_orphan_services: topological-sort-ordered initialization
- _init_* per-service initialization methods (fail-open behavior)
- _start_correlation_engine_loop: duplicate-start guard + enable branch
- _reset_all_background_state: 317 additional guard resets

Note: capacity_reservation/cell_topology/circuit_mesh were relocated to
baldur.bootstrap (framework-agnostic init()) — their startup tests now
live alongside other bootstrap helper tests.

599 D12: the correlation engine implementation lives in the private
distribution; apps.py start/init bodies resolve callables from
``ProviderRegistry.worker_background_starts``. The guard/flag contracts
are unchanged; patch targets below use the registry slot instead of the
relocated service module.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from baldur.adapters.django.apps import BaldurConfig
from baldur.factory.registry import ProviderRegistry

# =============================================================================
# Contract: _reset_all_background_state resets the 317 additional guards
# =============================================================================


class TestResetBackgroundState317Contract:
    """317: contract — _reset_all_background_state resets the new guards."""

    def test_resets_correlation_loop_guard(self):
        """_correlation_loop_started is reset."""
        BaldurConfig._correlation_loop_started = True
        BaldurConfig._reset_all_background_state()
        assert BaldurConfig._correlation_loop_started is False


# =============================================================================
# Behavior: _start_correlation_engine_loop duplicate prevention
# =============================================================================


class TestStartCorrelationEngineLoopBehavior:
    """317: _start_correlation_engine_loop behavior."""

    def setup_method(self):
        """Reset the guard before each test."""
        BaldurConfig._correlation_loop_started = False

    def test_skips_when_settings_disabled(self):
        """Disabled settings -> no start, flag stays False."""
        config = BaldurConfig.__new__(BaldurConfig)

        mock_settings = MagicMock()
        mock_settings.enabled = False

        calls = []
        slot = ProviderRegistry.worker_background_starts
        with (
            patch(
                "baldur.settings.correlation_engine.get_correlation_engine_settings",
                return_value=mock_settings,
            ),
            slot.snapshot(),
        ):
            slot.reset()
            slot.register("correlation_engine_loop", lambda: calls.append("loop"))
            config._start_correlation_engine_loop()

        assert BaldurConfig._correlation_loop_started is False
        assert calls == []

    def test_duplicate_start_prevented(self):
        """Already-started flag prevents a duplicate provider invocation."""
        config = BaldurConfig.__new__(BaldurConfig)
        BaldurConfig._correlation_loop_started = True

        mock_settings = MagicMock()
        mock_settings.enabled = True

        calls = []
        slot = ProviderRegistry.worker_background_starts
        with (
            patch(
                "baldur.settings.correlation_engine.get_correlation_engine_settings",
                return_value=mock_settings,
            ),
            slot.snapshot(),
        ):
            slot.reset()
            slot.register("correlation_engine_loop", lambda: calls.append("loop"))
            config._start_correlation_engine_loop()

        assert calls == []

    def test_enabled_with_provider_starts_loop(self):
        """Enabled + registered provider -> callable invoked, flag set."""
        config = BaldurConfig.__new__(BaldurConfig)

        mock_settings = MagicMock()
        mock_settings.enabled = True

        calls = []
        slot = ProviderRegistry.worker_background_starts
        with (
            patch(
                "baldur.settings.correlation_engine.get_correlation_engine_settings",
                return_value=mock_settings,
            ),
            slot.snapshot(),
        ):
            slot.reset()
            slot.register("correlation_engine_loop", lambda: calls.append("loop"))
            config._start_correlation_engine_loop()
            assert BaldurConfig._correlation_loop_started is True

        assert calls == ["loop"]

    def test_settings_import_error_handled_gracefully(self):
        """Settings lookup failure passes without raising; flag stays False."""
        config = BaldurConfig.__new__(BaldurConfig)

        with patch(
            "baldur.settings.correlation_engine.get_correlation_engine_settings",
            side_effect=ImportError("no module"),
        ):
            config._start_correlation_engine_loop()

        assert BaldurConfig._correlation_loop_started is False

    def test_generic_exception_handled_gracefully(self):
        """A crashing start callable warns and passes; flag rolls back."""
        config = BaldurConfig.__new__(BaldurConfig)

        mock_settings = MagicMock()
        mock_settings.enabled = True

        def _crashing_start() -> None:
            raise RuntimeError("engine broken")

        slot = ProviderRegistry.worker_background_starts
        with (
            patch(
                "baldur.settings.correlation_engine.get_correlation_engine_settings",
                return_value=mock_settings,
            ),
            slot.snapshot(),
        ):
            slot.reset()
            slot.register("correlation_engine_loop", _crashing_start)
            config._start_correlation_engine_loop()

        assert BaldurConfig._correlation_loop_started is False


# =============================================================================
# Behavior: _init_* per-service fail-open behavior
# =============================================================================


class TestInitOrphanServiceFailOpenBehavior:
    """317: per-service _init_* methods are fail-open."""

    def test_init_event_journal_import_error(self):
        """EventJournal import failure passes without raising."""
        with patch(
            "baldur.settings.event_journal.EventJournalSettings",
            side_effect=ImportError("no module"),
        ):
            BaldurConfig._init_event_journal()

    def test_init_event_journal_disabled(self):
        """Disabled EventJournal skips initialization."""
        mock_settings = MagicMock()
        mock_settings.enabled = False

        with patch(
            "baldur.settings.event_journal.EventJournalSettings",
            return_value=mock_settings,
        ):
            BaldurConfig._init_event_journal()

    def test_init_correlation_engine_settings_error(self):
        """CorrelationEngine settings failure passes without raising."""
        with patch(
            "baldur.settings.correlation_engine.get_correlation_engine_settings",
            side_effect=ImportError("no module"),
        ):
            BaldurConfig._init_correlation_engine()

    def test_init_correlation_engine_provider_absent(self):
        """Empty worker_background_starts slot -> debug no-op, no raise."""
        mock_settings = MagicMock()
        mock_settings.enabled = True

        slot = ProviderRegistry.worker_background_starts
        with (
            patch(
                "baldur.settings.correlation_engine.get_correlation_engine_settings",
                return_value=mock_settings,
            ),
            slot.snapshot(),
        ):
            slot.reset()
            BaldurConfig._init_correlation_engine()

    def test_init_correlation_engine_provider_invoked(self):
        """Enabled + registered provider -> init callable invoked."""
        mock_settings = MagicMock()
        mock_settings.enabled = True

        calls = []
        slot = ProviderRegistry.worker_background_starts
        with (
            patch(
                "baldur.settings.correlation_engine.get_correlation_engine_settings",
                return_value=mock_settings,
            ),
            slot.snapshot(),
        ):
            slot.reset()
            slot.register("correlation_engine_init", lambda: calls.append("init"))
            BaldurConfig._init_correlation_engine()

        assert calls == ["init"]

    def test_init_saga_autodiscover_import_error(self):
        """Celery import failure passes without raising."""
        with patch(
            "celery.current_app",
            side_effect=ImportError("no celery"),
            create=True,
        ):
            BaldurConfig._init_saga_autodiscover()

    def test_init_config_propagator_import_error(self):
        """ConfigPropagator import failure passes without raising."""
        with patch(
            "baldur.services.config.propagator.get_global_config_propagator",
            side_effect=ImportError("no module"),
        ):
            BaldurConfig._init_config_propagator()

    # _init_runbook tests moved to tests/pro/unit/test_register_relocated_features.py
    # (599 D12 - the runbook init seam relocated to register_pro_services).


# =============================================================================
# Behavior: _initialize_orphan_services integrated flow
# =============================================================================


class TestInitializeOrphanServicesBehavior:
    """317: _initialize_orphan_services integrated flow."""

    def test_calls_all_four_initializers(self):
        """All four initializer methods are called.

        runbook moved to PRO (599 D12); capacity_reservation init relocated to
        baldur.bootstrap's framework-agnostic starter.
        """
        config = BaldurConfig.__new__(BaldurConfig)
        call_order = []

        def make_tracker(name):
            def tracker():
                call_order.append(name)

            return tracker

        with (
            patch.object(config, "_init_event_journal", make_tracker("event_journal")),
            patch.object(
                config, "_init_correlation_engine", make_tracker("correlation_engine")
            ),
            patch.object(config, "_init_saga_autodiscover", make_tracker("saga")),
            patch.object(config, "_init_config_propagator", make_tracker("config")),
        ):
            config._initialize_orphan_services()

        assert len(call_order) == 4
        assert set(call_order) == {
            "event_journal",
            "correlation_engine",
            "saga",
            "config",
        }

    def test_exception_in_graph_does_not_crash(self):
        """A ServiceDependencyGraph exception does not abort initialization."""
        config = BaldurConfig.__new__(BaldurConfig)

        with patch(
            "baldur.core.dependency_graph.ServiceDependencyGraph",
            side_effect=RuntimeError("graph broken"),
        ):
            config._initialize_orphan_services()


# =============================================================================
# Behavior: _start_all_background_threads 317 additional calls
# =============================================================================


class TestStartAllBackgroundThreads317Behavior:
    """317: _start_all_background_threads invokes the 317-added methods."""

    def test_calls_correlation_method(self):
        """_start_all_background_threads calls correlation method."""
        config = BaldurConfig.__new__(BaldurConfig)

        with (
            patch(
                "baldur.adapters.django.apps.MetricHydrator.hydrate",
            ),
            patch.object(config, "_start_correlation_engine_loop") as mock_corr,
        ):
            config._start_all_background_threads()

        mock_corr.assert_called_once()
